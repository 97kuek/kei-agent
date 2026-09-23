"""研究時間の記録。

人の時間は Toggl（2.0）で自分で測り、Kei Agent の稼働時間は `runs` テーブルから数える。
その2つを日ごと・領域ごとに並べた CSV を `<agent_root>/overview/time/` に書き、
グラフは Claude に作ってもらう（Kei Agent は材料を用意するところまで）。

Toggl の鍵（`toggl_sk_...`）は環境変数 `TOGGL_API_TOKEN` から、宛先の組織とワークスペースの ID は
`TOGGL_ORGANIZATION_ID` と `TOGGL_WORKSPACE_ID` から読む。どれかがなければ人の時間は空欄になる。
ID は API から調べる手段がないので、Toggl を開いたときの URL（`/<組織>/workspaces/<ワークスペース>/`）から写す。
"""

from __future__ import annotations

import csv
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kei_agent.config import Config
from kei_agent.store import Store
from kei_agent.themes import theme_dirs

log = logging.getLogger(__name__)

# 従来の Toggl Track（api.track.toggl.com/api/v9）は、2.0 の鍵では 401 になる
TOGGL_API = "https://focus.toggl.com/api"
PER_PAGE = 100
# ページ送りが止まらなかったときの上限。1週間分でここまで行くことはない
MAX_PAGES = 50
TIME_DIR = "time"
# Toggl のプロジェクト名の先頭に付ける印。ここに挙げた領域だけを数え、印のないもの
# （アルバイト、個人開発）は捨てる。科目やテーマが増えても、このコードは変えなくてよい
DOMAINS = ("研究", "大学", "仕事")
DOMAIN_SEP = "/"
# テーマのチャンネル以外（研究全体、Kei Agent の改善）で動いた分をまとめる名前
OTHER = "そのほか"


class TogglError(RuntimeError):
    pass


class TogglAmbiguousWrite(TogglError):
    """送信後に接続が切れた可能性があり、二重送信を避けるべき失敗。"""


def week_start(day: date) -> date:
    """その日を含む週の月曜。"""
    return day - timedelta(days=day.weekday())


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).date().isoformat()


def split_project(name: str) -> tuple[str, str] | None:
    """Toggl のプロジェクト名を（領域, 名前）に分ける。印が無ければ None（数えない）。"""
    domain, sep, rest = (name or "").partition(DOMAIN_SEP)
    if not sep or domain.strip() not in DOMAINS or not rest.strip():
        return None
    return domain.strip(), rest.strip()


def assistant_seconds(store: Store, since: float, until: float) -> dict[tuple[str, str], float]:
    """Kei Agent が動いていた秒数。(日付, テーマ) ごとに合計する。

    終わっていない実行（ended_at が空）は数えない。
    """
    rows = store.conn.execute(
        "SELECT channel_name, started_at, ended_at FROM runs "
        "WHERE ended_at IS NOT NULL AND started_at >= ? AND started_at < ?",
        (since, until),
    ).fetchall()
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        totals[(_day(r["started_at"]), r["channel_name"] or "-")] += r["ended_at"] - r["started_at"]
    return dict(totals)


class Toggl:
    """Toggl 2.0 の API。自分の時間の記録を読むだけに使う。"""

    def __init__(self, token: str, organization_id: int, workspace_id: int):
        self.token = token
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self._project_ids: dict[str, int] = {}

    def _request(self, path: str, query: dict) -> dict:
        url = f"{TOGGL_API}{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise TogglError(f"GET {path}: {e.code} {e.read().decode('utf-8', 'replace')[:200]}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise TogglError(f"GET {path}: {e}") from None

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{TOGGL_API}{path}", method="POST", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as e:
            raise TogglError(f"POST {path}: {e.code} {e.read().decode('utf-8', 'replace')[:200]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise TogglAmbiguousWrite(f"POST {path}: {type(e).__name__}") from None
        except json.JSONDecodeError as e:
            raise TogglError(f"POST {path}: JSONDecodeError: {e}") from None

    def _project_id(self, name: str) -> int:
        if name in self._project_ids:
            return self._project_ids[name]
        path = f"/organizations/{self.organization_id}/workspaces/{self.workspace_id}/projects"
        for page in range(1, MAX_PAGES + 1):
            projects = self._request(path, {"page": page, "per_page": PER_PAGE}).get("data") or []
            for project in projects:
                if project.get("name") == name and isinstance(project.get("id"), int):
                    self._project_ids[name] = project["id"]
                    return project["id"]
            if len(projects) < PER_PAGE:
                break
        created = self._post(path, {"name": name})
        project = created.get("data") if isinstance(created.get("data"), dict) else created
        project_id = project.get("id") if isinstance(project, dict) else None
        if not isinstance(project_id, int):
            raise TogglError("Toggl が作ったプロジェクトIDを返しませんでした")
        self._project_ids[name] = project_id
        return project_id

    def record_completed(self, project_name: str, description: str, started_at: datetime,
                         duration_seconds: int) -> None:
        """止めた時点で確定した、タスクなしの時間を1件だけ入れる。"""
        if not project_name.strip() or not description.strip() or duration_seconds <= 0:
            raise ValueError("プロジェクト、説明、正の時間を指定してください")
        if started_at.tzinfo is None:
            raise ValueError("開始時刻にはタイムゾーンが必要です")
        project_id = self._project_id(project_name.strip())
        path = f"/organizations/{self.organization_id}/workspaces/{self.workspace_id}/time-entries/bulk"
        self._post(path, {"items": [{
            "project_id": project_id,
            "description": description.strip(),
            "start": started_at.isoformat(),
            "duration": int(duration_seconds),
            "type": "activity",
        }]})

    def entries(self, since: date, until: date) -> list[dict]:
        """since 〜 until（両端を含む、手元の日付）の記録。"""
        path = f"/organizations/{self.organization_id}/workspaces/{self.workspace_id}/time-entries"
        query = {
            "date_from": _utc(since),
            "date_to": _utc(until + timedelta(days=1)),
            # 付けないと、タスクなしでプロジェクトだけ選んで測った記録が1件も返らない
            "include_taskless": "true",
            "per_page": PER_PAGE,
        }
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data = self._request(path, query | {"page": page}).get("data") or []
            rows += data
            if len(data) < PER_PAGE:
                return rows
        log.warning("Toggl の記録が %d ページを超えたので、そこで打ち切った", MAX_PAGES)
        return rows


def _utc(day: date) -> str:
    """手元の日付の 0 時を、API が受け取る UTC の時刻にする。"""
    return datetime.combine(day, datetime.min.time()).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_seconds(entries: list[dict]) -> dict[tuple[str, str], float]:
    """人が作業していた秒数。(日付, テーマ) ごとに合計する。テーマはプロジェクト名。

    動かしっぱなしの記録（duration が空か負）は、まだ終わっていないので数えない。休憩と消した記録も数えない。
    """
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for e in entries:
        duration = e.get("duration")
        start = e.get("start")
        if not start or not isinstance(duration, int | float) or duration <= 0:
            continue
        if e.get("type") == "break" or e.get("deleted_at"):
            continue
        day = datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone().date().isoformat()
        theme = (e.get("project") or {}).get("name") or "-"
        totals[(day, theme)] += float(duration)
    return dict(totals)


def load_toggl(env: dict[str, str] | None = None) -> Toggl | None:
    env = dict(os.environ) if env is None else env
    token = env.get("TOGGL_API_TOKEN")
    if not token:
        log.info("TOGGL_API_TOKEN がないので、人の研究時間は数えない")
        return None
    try:
        return Toggl(token, int(env["TOGGL_ORGANIZATION_ID"]), int(env["TOGGL_WORKSPACE_ID"]))
    except (KeyError, ValueError):
        log.warning("TOGGL_ORGANIZATION_ID と TOGGL_WORKSPACE_ID が数字で入っていないので、人の研究時間は数えない")
        return None


def write_week(config: Config, store: Store, toggl: Toggl | None, day: date | None = None) -> Path:
    """その週の材料を CSV に書く。Claude はこれを読んでグラフを作る。"""
    monday = week_start(day or date.today())
    sunday = monday + timedelta(days=6)
    since = datetime.combine(monday, datetime.min.time()).timestamp()
    until = datetime.combine(sunday + timedelta(days=1), datetime.min.time()).timestamp()

    themes = {d.name for d in theme_dirs(config)}
    # Kei Agent が動くのは研究だけ。テーマのチャンネル以外はまとめる
    agent: dict[tuple[str, str, str], float] = defaultdict(float)
    for (day_, name), seconds in assistant_seconds(store, since, until).items():
        agent[(day_, "研究", name if name in themes else OTHER)] += seconds
    human: dict[tuple[str, str, str], float] = defaultdict(float)
    if toggl is not None:
        try:
            # Toggl にはアルバイトや個人開発の時間も入っている。印の付いたものだけを数える
            for (day_, project), seconds in human_seconds(toggl.entries(monday, sunday)).items():
                found = split_project(project)
                if found:
                    human[(day_, found[0], found[1])] += seconds
        except TogglError as e:
            log.warning("Toggl から読めません: %s", e)

    path = config.overview_dir / TIME_DIR / f"{monday.isoformat()}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["日付", "領域", "プロジェクト", "人の時間（分）", "Kei Agent の稼働（分）"])
        for key in sorted(set(agent) | set(human)):
            writer.writerow([*key, round(human.get(key, 0) / 60, 1), round(agent.get(key, 0) / 60, 1)])
    return path


def week_summary(path: Path) -> list[str]:
    """CSV から、Daily の材料に入れる短い要約を作る。"""
    if not path.exists():
        return ["- まだ記録がない"]
    human = agent = 0.0
    by_domain: dict[str, float] = defaultdict(float)
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            human += float(row["人の時間（分）"])
            agent += float(row["Kei Agent の稼働（分）"])
            # 先週までの CSV には「領域」の列がない
            by_domain[row.get("領域") or "研究"] += float(row["人の時間（分）"])
    lines = [f"- 今週の合計: 人 {human / 60:.1f} 時間 / Kei Agent {agent / 60:.1f} 時間", f"- 材料: `{path}`"]
    for domain, minutes in sorted(by_domain.items(), key=lambda kv: -kv[1]):
        if minutes:
            lines.append(f"  - {domain}: {minutes / 60:.1f} 時間")
    if human == 0:
        lines.append("- 人の時間が0。Toggl を回していないか、プロジェクト名に "
                     f"`{'/`・`'.join(DOMAINS)}/` の印が付いていないか、`TOGGL_*` の環境変数が足りない")
    return lines
