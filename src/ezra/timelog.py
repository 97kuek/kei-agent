"""研究時間の記録（フェーズ5）。

人の時間は Toggl Track で自分で測り、Ezra の稼働時間は `runs` テーブルから数える。
その2つを日ごと・テーマごとに並べた CSV を `_overview/time/` に書き、
グラフは Claude に作ってもらう（Ezra は材料を用意するところまで）。

Toggl のトークンは環境変数 `TOGGL_API_TOKEN` から読む。なければ人の時間は空欄になる。
"""

from __future__ import annotations

import base64
import csv
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from ezra.config import Config
from ezra.store import Store
from ezra.themes import OVERVIEW_DIR

log = logging.getLogger(__name__)

TOGGL_API = "https://api.track.toggl.com/api/v9"
TIME_DIR = "time"


class TogglError(RuntimeError):
    pass


def week_start(day: date) -> date:
    """その日を含む週の月曜。"""
    return day - timedelta(days=day.weekday())


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).date().isoformat()


def assistant_seconds(store: Store, since: float, until: float) -> dict[tuple[str, str], float]:
    """Ezra が動いていた秒数。(日付, テーマ) ごとに合計する。

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
    """Toggl Track の API v9。自分の時間の記録を読むだけに使う。"""

    def __init__(self, token: str):
        self.token = token

    def _request(self, path: str, query: dict) -> list[dict]:
        credential = base64.b64encode(f"{self.token}:api_token".encode()).decode()
        url = f"{TOGGL_API}{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {credential}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise TogglError(f"GET {path}: {e.code} {e.read().decode('utf-8', 'replace')[:200]}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise TogglError(f"GET {path}: {e}") from None

    def entries(self, since: date, until: date) -> list[dict]:
        """since 〜 until（両端を含む）の記録。"""
        return self._request("/me/time_entries", {
            "start_date": since.isoformat(),
            "end_date": f"{until.isoformat()}T23:59:59Z",
        })

    def projects(self) -> dict[int, str]:
        """プロジェクトIDから名前への対応。テーマ名に合わせてある想定。"""
        rows = self._request("/me/projects", {})
        return {int(p["id"]): p.get("name") or "-" for p in rows if p.get("id") is not None}


def human_seconds(entries: list[dict], projects: dict[int, str]) -> dict[tuple[str, str], float]:
    """人が作業していた秒数。(日付, テーマ) ごとに合計する。

    動かしっぱなしの記録（duration が負）は、まだ終わっていないので数えない。
    """
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for e in entries:
        duration = e.get("duration")
        start = e.get("start")
        if not start or not isinstance(duration, int | float) or duration <= 0:
            continue
        day = datetime.fromisoformat(str(start)).astimezone().date().isoformat()
        theme = projects.get(e.get("project_id"), "-") if e.get("project_id") else "-"
        totals[(day, theme)] += float(duration)
    return dict(totals)


def load_toggl(env: dict[str, str] | None = None) -> Toggl | None:
    env = dict(os.environ) if env is None else env
    token = env.get("TOGGL_API_TOKEN")
    if not token:
        log.info("TOGGL_API_TOKEN がないので、人の研究時間は数えない")
        return None
    return Toggl(token)


def write_week(config: Config, store: Store, toggl: Toggl | None, day: date | None = None) -> Path:
    """その週の材料を CSV に書く。Claude はこれを読んでグラフを作る。"""
    monday = week_start(day or date.today())
    sunday = monday + timedelta(days=6)
    since = datetime.combine(monday, datetime.min.time()).timestamp()
    until = datetime.combine(sunday + timedelta(days=1), datetime.min.time()).timestamp()

    ezra = assistant_seconds(store, since, until)
    human: dict[tuple[str, str], float] = {}
    if toggl is not None:
        try:
            human = human_seconds(toggl.entries(monday, sunday), toggl.projects())
        except TogglError as e:
            log.warning("Toggl から読めません: %s", e)

    path = config.research_root / OVERVIEW_DIR / TIME_DIR / f"{monday.isoformat()}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["日付", "テーマ", "人の時間（分）", "Ezra の稼働（分）"])
        for key in sorted(set(ezra) | set(human)):
            writer.writerow([key[0], key[1], round(human.get(key, 0) / 60, 1), round(ezra.get(key, 0) / 60, 1)])
    return path


def week_summary(path: Path) -> list[str]:
    """CSV から、Daily の材料に入れる短い要約を作る。"""
    if not path.exists():
        return ["- まだ記録がない"]
    human = ezra = 0.0
    by_theme: dict[str, float] = defaultdict(float)
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            human += float(row["人の時間（分）"])
            ezra += float(row["Ezra の稼働（分）"])
            by_theme[row["テーマ"]] += float(row["人の時間（分）"])
    lines = [f"- 今週の合計: 人 {human / 60:.1f} 時間 / Ezra {ezra / 60:.1f} 時間", f"- 材料: `{path}`"]
    for theme, minutes in sorted(by_theme.items(), key=lambda kv: -kv[1]):
        if minutes:
            lines.append(f"  - {theme}: {minutes / 60:.1f} 時間")
    if human == 0:
        lines.append("- 人の時間が0。Toggl を回していないか、`TOGGL_API_TOKEN` が設定されていない")
    return lines
