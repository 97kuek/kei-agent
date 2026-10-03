"""Moodle の認証付き REST API。提出・受験終了の読み取りだけを扱う。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

TIMEOUT_SECONDS = 5


class APIError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # トークンを別の URL に転送しない。正規の Moodle URL を設定してもらう。
        raise APIError("Moodle API が転送されました。MOODLE_API_URL を確認してください")


@dataclass(frozen=True)
class Completion:
    completed: bool
    url: str


def _positive(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 and str(number) == str(value) else None


class Client:
    def __init__(self, root: str, token: str):
        try:
            parsed = urlsplit(root)
            valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment and parsed.port in (None, 443))
        except ValueError:
            valid = False
        if not valid or not token.strip():
            raise APIError("MOODLE_API_URL は HTTPS の Moodle のルート URL、MOODLE_API_TOKEN は必須です")
        self.root = root.rstrip("/")
        self.origin = (parsed.scheme, parsed.hostname, parsed.port or 443)
        self.path = parsed.path.rstrip("/")
        self._token = token

    def call(self, function: str, **params) -> dict:
        body = urlencode({**params, "wstoken": self._token, "wsfunction": function,
                          "moodlewsrestformat": "json"}).encode()
        request = urllib.request.Request(self.root + "/webservice/rest/server.php", data=body, method="POST")
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
                result = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError):
            # API の message や URL を含む例外には秘密情報が混じることがある。
            raise APIError(f"Moodle API を読めません（{function}）") from None
        if not isinstance(result, dict) or result.get("exception") or result.get("errorcode") or result.get("warnings"):
            raise APIError(f"Moodle API が取得を拒否しました（{function}）。トークンと利用可能な関数を確認してください")
        return result

    def _activity(self, url: str) -> tuple[str, int] | None:
        try:
            parsed = urlsplit(url)
            if ((parsed.scheme, parsed.hostname, parsed.port or 443) != self.origin
                    or parsed.username or parsed.password):
                return None
        except ValueError:
            return None
        kind = next((kind for kind in ("assign", "quiz")
                     if parsed.path == f"{self.path}/mod/{kind}/view.php"), None)
        ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        cmid = _positive(ids[0]) if len(ids) == 1 else None
        return (kind, cmid) if kind and cmid else None

    def completion(self, url: str, uid: str) -> Completion | None:
        if not url:
            event, separator, site = uid.partition("@")
            eventid = _positive(event)
            # Moodle の UID は wwwroot の scheme 以外を含む。サブパスも区別する。
            try:
                parsed = urlsplit("https://" + site)
                same_site = ((parsed.scheme, parsed.hostname, parsed.port or 443) == self.origin
                             and parsed.path.rstrip("/") == self.path
                             and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
            except ValueError:
                same_site = False
            if not separator or not same_site or not eventid:
                return None
            data = self.call("core_calendar_get_calendar_event_by_id", eventid=eventid).get("event")
            if not isinstance(data, dict):
                raise APIError("Moodle のカレンダーイベントがありません")
            if data.get("id") != eventid:
                raise APIError("Moodle のカレンダーイベント ID が一致しません")
            url = data.get("url") or ""
            if not isinstance(url, str):
                raise APIError("Moodle の活動 URL が不正です")
        activity = self._activity(url)
        if activity is None:
            return None
        kind, cmid = activity
        canonical = f"{self.root}/mod/{kind}/view.php?id={cmid}"
        cm = self.call("core_course_get_course_module", cmid=cmid).get("cm")
        if not isinstance(cm, dict):
            raise APIError("Moodle の活動がありません")
        instance = _positive(cm.get("instance"))
        if cm.get("id") != cmid or cm.get("modname") != kind or not instance:
            raise APIError("Moodle の活動 ID または種類が一致しません")
        if kind == "quiz":
            attempts = self.call("mod_quiz_get_user_attempts", quizid=instance, userid=0,
                                 status="all", includepreviews=0).get("attempts")
            if not isinstance(attempts, list):
                raise APIError("Moodle の受験状態がありません")
            completed = any(isinstance(a, dict) and not a.get("preview")
                            and a.get("quiz", instance) == instance
                            and a.get("state") in ("finished", "submitted") for a in attempts)
        else:
            attempt = self.call("mod_assign_get_submission_status", assignid=instance, userid=0).get("lastattempt")
            if not isinstance(attempt, dict):
                raise APIError("Moodle の提出状態がありません")
            own = (isinstance(attempt.get("submission"), dict)
                   and attempt["submission"].get("status") == "submitted")
            team = (isinstance(attempt.get("teamsubmission"), dict)
                    and attempt["teamsubmission"].get("status") == "submitted"
                    and attempt.get("submissiongroupmemberswhoneedtosubmit") == [])
            completed = own or team
        return Completion(completed, canonical)


def from_env(env: dict[str, str] | None = None) -> Client | None:
    env = os.environ if env is None else env
    root, token = env.get("MOODLE_API_URL", ""), env.get("MOODLE_API_TOKEN", "")
    if not root and not token:
        return None
    return Client(root, token)
