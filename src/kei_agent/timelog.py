"""Toggl と、時間の数え方。

人の時間は Slack の時間記録カードか Toggl（2.0）で測り、共通 Notion ホームの「時間記録」に1件ずつ入れる。
週ごとの合計は Notion のグラフで見る。Kei Agent の稼働時間は `runs` テーブルから数える（手元の SQLite）。
Toggl のアプリで直接測った記録は、毎晩の保守で「時間記録」に取り込む（`import_toggl`）。

Toggl の鍵（`toggl_sk_...`）は環境変数 `TOGGL_API_TOKEN` から、宛先の組織とワークスペースの ID は
`TOGGL_ORGANIZATION_ID` と `TOGGL_WORKSPACE_ID` から読む。どれかがなければ Toggl には送らず、取り込みもしない。
ID は API から調べる手段がないので、Toggl を開いたときの URL（`/<組織>/workspaces/<ワークスペース>/`）から写す。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from kei_agent.store import Store

log = logging.getLogger(__name__)

# 従来の Toggl Track（api.track.toggl.com/api/v9）は、2.0 の鍵では 401 になる
TOGGL_API = "https://focus.toggl.com/api"
PER_PAGE = 100
# ページ送りが止まらなかったときの上限。1週間分でここまで行くことはない
MAX_PAGES = 50
# Toggl のプロジェクト名の先頭に付ける印。ここに挙げた領域だけを数え、印のないもの
# （アルバイト、個人開発）は捨てる。科目やテーマが増えても、このコードは変えなくてよい
DOMAINS = ("研究", "大学", "仕事")
DOMAIN_SEP = "/"
# Toggl のアプリで直接測った記録を、何日さかのぼって取り込むか（保守が何晩か止まっても埋まるように）
IMPORT_DAYS = 7
# Slack から送った記録と同じとみなす、開始と長さのずれ（秒）
SAME_ENTRY_SECONDS = 60


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
    rows = store.finished_runs(since, until)
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

    def _post(self, path: str, body: dict | list) -> dict:
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
        # bulk は本文に配列そのものを受け取る（{"items": ...} の形だと 400 になる）
        self._post(path, [{
            "project_id": project_id,
            "description": description.strip(),
            "start": started_at.isoformat(),
            "duration": int(duration_seconds),
            "type": "activity",
        }])

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


def load_toggl(env: dict[str, str] | None = None) -> Toggl | None:
    env = dict(os.environ) if env is None else env
    token = env.get("TOGGL_API_TOKEN")
    if not token:
        log.info("TOGGL_API_TOKEN がないので、Toggl には送らない")
        return None
    try:
        return Toggl(token, int(env["TOGGL_ORGANIZATION_ID"]), int(env["TOGGL_WORKSPACE_ID"]))
    except (KeyError, ValueError):
        log.warning("TOGGL_ORGANIZATION_ID と TOGGL_WORKSPACE_ID が数字で入っていないので、Toggl には送らない")
        return None


def import_toggl(toggl: Toggl, hub, own: list[tuple[float, float]], since: date, until: date) -> dict:
    """Toggl で直接測った記録を、共通ホームの「時間記録」に記録 ID「toggl:<id>」で入れる。

    own は Slack で測った記録の（開始の時刻, 秒数）。開始と長さがどちらも SAME_ENTRY_SECONDS 以内で
    重なる Toggl の記録は、Slack から送った同じものなので飛ばす。入れ済みの ID、計測中、休憩、
    消した記録、印のないプロジェクトも飛ばす。
    """
    # Notion の日付の絞り込みは時差の分ずれることがあるので、1日広く取る
    known = hub.time_ids_since(since - timedelta(days=1))
    counts = {"imported": 0, "own": 0, "known": 0, "unmarked": 0}
    for e in toggl.entries(since, until):
        duration, start = e.get("duration"), e.get("start")
        if (e.get("id") is None or not start or not isinstance(duration, int | float) or duration <= 0
                or e.get("type") == "break" or e.get("deleted_at")):
            continue
        project = str((e.get("project") or {}).get("name") or "")
        found = split_project(project)
        if found is None:
            counts["unmarked"] += 1
            continue
        started = datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone()
        if any(abs(started.timestamp() - at) <= SAME_ENTRY_SECONDS
               and abs(duration - seconds) <= SAME_ENTRY_SECONDS for at, seconds in own):
            counts["own"] += 1
            continue
        entry_id = f"toggl:{e['id']}"
        if entry_id in known:
            counts["known"] += 1
            continue
        description = str(e.get("description") or "").strip()
        hub.record_time(entry_id, found[0], found[1], started.isoformat(), max(1, round(duration / 60)),
                        "" if description == project.strip() else description, "", "Toggl")
        counts["imported"] += 1
    return {"status": "done", **counts}
