"""Mac の Outlook から、会社の予定を読む（AppleScript）。

会社のテナントには何も登録しない方法。Outlook.app に AppleScript で聞くだけなので、
アプリの登録も管理者の承認も要らず、予定のデータは Mac の外に出ない。

- **「新しい Outlook」では使えない**（AppleScript から見えるのは従来のプロファイルだけで、中身が空になる）。
  使うには Outlook のメニューから「従来の Outlook」に戻す必要がある
- 初回だけ「Outlook を制御することを許可しますか」の確認が macOS から出る
- Outlook.app が動いている必要がある（止まっていれば AppleScript が起動する）
- Entra ID のアプリが使えるようになったら `graph.py` に切り替えられる（口は同じ形）

読むのは件名・時間・場所・主催者まで。本文（content）は読まない（docs/plan.md の15章）。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime

log = logging.getLogger(__name__)

# 予定を見る先の長さ（日）
DEFAULT_DAYS = 7
MAX_EVENTS = 60
TIMEOUT_SECONDS = 60
# AppleScript の中で使う区切り（予定の本文には出てこない字）
RECORD, FIELD = "\x1e", "\x1f"
NO_OUTLOOK = ("Mac の Outlook から予定を読めませんでした。Outlook.app が入っていて、"
              "「システム設定 → プライバシーとセキュリティ → オートメーション」で許可されているか確かめてください")

SCRIPT = f"""
on two(n)
	set s to "0" & (n as integer)
	return text -2 thru -1 of s
end two

on iso(d)
	set y to (year of d as integer) as string
	return y & "-" & two(month of d as integer) & "-" & two(day of d) & "T" & two(hours of d) & ":" & two(minutes of d)
end iso

on run argv
	set ahead to (item 1 of argv) as integer
	set d1 to (current date)
	set d2 to d1 + ahead * days
	set out to ""
	tell application "Microsoft Outlook"
		repeat with c in calendars
			repeat with e in (every calendar event of c whose start time is greater than or equal to d1 and start time is less than or equal to d2)
				set out to out & (subject of e) & "{FIELD}" & my iso(start time of e) & "{FIELD}" & ¬
					my iso(end time of e) & "{FIELD}" & (location of e) & "{FIELD}" & ¬
					((all day flag of e) as string) & "{FIELD}" & (organizer of e) & "{FIELD}" & ¬
					((free busy status of e) as string) & "{RECORD}"
			end repeat
		end repeat
	end tell
	return out
end run
"""


class OutlookError(RuntimeError):
    pass


def _parse(raw: str) -> list[dict]:
    events = []
    for record in raw.split(RECORD):
        parts = record.split(FIELD)
        if len(parts) < 7 or not parts[1].strip():
            continue
        subject, start, end, location, all_day, organizer, status = (p.strip() for p in parts[:7])
        events.append({
            "subject": subject or "（件名なし）",
            "start": start,
            "end": end,
            "all_day": all_day.lower() == "true",
            "location": location,
            "organizer": organizer,
            "free": status.lower() == "free",
            "url": "",
        })
    events.sort(key=lambda e: e["start"])
    return events[:MAX_EVENTS]


def events(days: int = DEFAULT_DAYS, script: str = SCRIPT) -> list[dict]:
    """これから days 日ぶんの予定を、始まる順に。"""
    try:
        done = subprocess.run(["osascript", "-", str(max(days, 1))], input=script, capture_output=True,
                              text=True, timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OutlookError(f"{NO_OUTLOOK}（{type(e).__name__}）") from None
    if done.returncode:
        raise OutlookError(f"{NO_OUTLOOK}: {done.stderr.strip()[:300]}")
    found = _parse(done.stdout)
    log.info("Outlook から予定を %d 件読みました（%d 日ぶん）", len(found), days)
    return found


async def events_async(days: int = DEFAULT_DAYS) -> list[dict]:
    return await asyncio.to_thread(events, days)


def today(events_: list[dict], now: datetime | None = None) -> list[dict]:
    """今日ぶんだけ（朝のまとめで使う）。"""
    day = (now or datetime.now()).date().isoformat()
    return [e for e in events_ if e["start"].startswith(day)]
