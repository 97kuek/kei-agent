"""提出・受験終了の判定と Notion 更新。実際の Moodle・Notion は使わない。"""

import io
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import pytest

pytest.importorskip("a2a")

from test_course_sync import QUIZ, REPORT, STATE, FakeNotion, _row

from kei_agent_modules.course import moodle_api, notion_sync, submissions
from kei_agent_modules.course.notion_setup import IN_PROGRESS, NOT_STARTED, OVERDUE, SUBMITTED

ROOT = "https://moodle.example/moodle"


class FakeAPI(moodle_api.Client):
    def __init__(self, state="finished", kind="quiz", root=ROOT):
        super().__init__(root, "test-token")
        self.state, self.kind, self.calls = state, kind, []

    def call(self, function, **params):
        self.calls.append((function, params))
        if function == "core_calendar_get_calendar_event_by_id":
            return {"event": {"id": params["eventid"], "url": self.root + "/mod/quiz/view.php?id=42"}}
        if function == "core_course_get_course_module":
            return {"cm": {"id": params["cmid"], "modname": self.kind, "instance": 7}}
        if function == "mod_quiz_get_user_attempts":
            return {"attempts": [{"quiz": 7, "state": self.state, "preview": 0}]}
        if function == "mod_assign_get_submission_status":
            return {"lastattempt": {"submission": {"status": self.state}}}
        pytest.fail(function)


@pytest.mark.parametrize(("state", "complete"), [
    ("finished", True), ("submitted", True), ("inprogress", False),
    ("overdue", False), ("abandoned", False),
])
def test_quiz_attempt_completion(state, complete):
    api = FakeAPI(state)
    assert api.completion(ROOT + "/mod/quiz/view.php?id=42", "123@moodle.example/moodle").completed is complete
    assert api.calls == [
        ("core_course_get_course_module", {"cmid": 42}),
        ("mod_quiz_get_user_attempts", {"quizid": 7, "userid": 0, "status": "all", "includepreviews": 0}),
    ]


@pytest.mark.parametrize(("state", "complete"), [("submitted", True), ("draft", False), ("new", False)])
def test_assignment_submission_completion(state, complete):
    api = FakeAPI(state, "assign")
    assert api.completion(ROOT + "/mod/assign/view.php?id=42", "").completed is complete
    assert api.calls[-1] == ("mod_assign_get_submission_status", {"assignid": 7, "userid": 0})


@pytest.mark.parametrize("attempts", [[], [{"quiz": 7, "state": "finished", "preview": 1}],
                                     [{"quiz": 8, "state": "finished"}]])
def test_preview_and_other_quiz_attempts_are_not_completion(attempts):
    class QuizAPI(FakeAPI):
        def call(self, function, **params):
            return {"attempts": attempts} if function == "mod_quiz_get_user_attempts" else super().call(function, **params)

    assert not QuizAPI().completion(ROOT + "/mod/quiz/view.php?id=42", "").completed


@pytest.mark.parametrize(("pending", "complete"), [([], True), ([123], False), (None, False)])
def test_group_requires_completed_group_submission_and_no_pending_members(pending, complete):
    class GroupAPI(FakeAPI):
        def call(self, function, **params):
            if function == "mod_assign_get_submission_status":
                return {"lastattempt": {"teamsubmission": {"status": "submitted"},
                                        "submissiongroupmemberswhoneedtosubmit": pending}}
            return super().call(function, **params)

    assert GroupAPI(kind="assign").completion(ROOT + "/mod/assign/view.php?id=42", "").completed is complete


@pytest.mark.parametrize("root", [ROOT, "https://moodle.example"])
def test_missing_quiz_url_is_resolved_by_calendar_event_not_title(root):
    api = FakeAPI(root=root)
    outcome = api.completion("", "123@" + root.removeprefix("https://"))
    assert outcome.completed and outcome.url == root + "/mod/quiz/view.php?id=42"
    assert api.calls[0] == ("core_calendar_get_calendar_event_by_id", {"eventid": 123})


@pytest.mark.parametrize(("url", "uid"), [
    ("https://other.example/moodle/mod/quiz/view.php?id=42", "123@moodle.example/moodle"),
    (ROOT + "/mod/quiz/view.php?id=42&id=43", ""),
    (ROOT + "/mod/quiz/view.php?id=42&id=", ""),
    (ROOT + "/mod/forum/view.php?id=42", ""),
    ("", "123@other.example"), ("", "q1@moodle.example/moodle"),
    ("", "123@moodle.example"), ("", "123@moodle.example/other"),
    ("https://moodle.example/other/mod/quiz/view.php?id=42", ""),
])
def test_ambiguous_or_foreign_identity_does_not_call_api(url, uid):
    api = FakeAPI()
    assert api.completion(url, uid) is None
    assert api.calls == []


def test_transport_posts_token_in_body_and_sanitizes_failures(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return io.BytesIO(json.dumps({"exception": "error", "message": "private-response"}).encode())

    monkeypatch.setattr(moodle_api.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(moodle_api.APIError) as error:
        moodle_api.Client(ROOT, "test-token").call("core_course_get_course_module", cmid=42)
    request, = calls
    assert request.full_url == ROOT + "/webservice/rest/server.php"
    assert request.method == "POST"
    assert parse_qs(request.data.decode())["wstoken"] == ["test-token"]
    assert "test-token" not in str(error.value) and "private-response" not in str(error.value)


def test_transport_rejects_redirects():
    with pytest.raises(moodle_api.APIError, match="転送"):
        moodle_api._NoRedirect().redirect_request(None, None, 307, "", {}, "https://other.example/")


@pytest.mark.parametrize("env", [
    {"MOODLE_API_URL": ROOT}, {"MOODLE_API_TOKEN": "test-token"},
    {"MOODLE_API_URL": "http://moodle.example", "MOODLE_API_TOKEN": "test-token"},
    {"MOODLE_API_URL": ROOT + "?private=query", "MOODLE_API_TOKEN": "test-token"},
])
def test_partial_or_invalid_settings_fail_without_echoing_values(env):
    with pytest.raises(moodle_api.APIError) as error:
        moodle_api.from_env(env)
    assert "test-token" not in str(error.value) and "private=query" not in str(error.value)


def test_disabled_api_does_not_access_notion(monkeypatch):
    monkeypatch.delenv("MOODLE_API_URL", raising=False)
    monkeypatch.delenv("MOODLE_API_TOKEN", raising=False)
    monkeypatch.setattr(notion_sync, "_client", lambda: pytest.fail("未設定なら Notion も読まない"))
    assert submissions.sync().enabled is False


class MutableNotion(FakeNotion):
    def request(self, method, path, body=None):
        reply = super().request(method, path, body)
        if method == "PATCH" and path.startswith("/pages/"):
            row = next(row for row in self.rows["ds-assignments"] if row["id"] == path.split("/")[-1])
            row["properties"].update(body["properties"])
        return reply


def client_for(*rows):
    notion = MutableNotion(assignments=rows)
    return notion_sync.CourseNotion(notion, STATE), notion


def quiz_row():
    row = _row(QUIZ)
    row["properties"]["Moodle ID"] = {"rich_text": [{"plain_text": "123@moodle.example/moodle"}]}
    row["properties"]["Status"] = {"status": {"name": IN_PROGRESS}}
    return row


def test_completed_quiz_updates_only_status_and_missing_link_once():
    row = quiz_row()
    client, notion = client_for(row)
    api = FakeAPI()
    first = submissions.sync(api=api, client=client)
    assert first.completed == ["Short test 1"] and first.checked == 1
    assert notion.calls == [("PATCH", "/pages/row-1", {"properties": {
        "Status": {"status": {"name": "Submitted"}},
        "Link": {"url": ROOT + "/mod/quiz/view.php?id=42"},
    }})]
    assert submissions.sync(api=api, client=client).completed == []
    assert len(notion.calls) == 1


@pytest.mark.parametrize("status", [SUBMITTED, OVERDUE])
def test_submitted_and_overdue_assignments_are_left_out_of_the_due_list(status):
    """手で Overdue にした課題や Submitted の課題は、Due がまだ先でも締切一覧に出さない。"""
    from kei_agent_modules.course.agent import due_data

    now = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=9)))
    rows = [{"id": "kept", "title": "レポート", "due": "2026-10-05T23:59:00+09:00", "status": NOT_STARTED},
            {"id": "dropped", "title": "手で閉じた課題", "due": "2026-10-04T23:59:00+09:00", "status": status}]
    assert [item["id"] for item in due_data(rows, 14, now=now)["items"]] == ["kept"]


def test_unfinished_and_failed_checks_preserve_manual_progress():
    client, notion = client_for(quiz_row())
    assert not submissions.sync(api=FakeAPI("inprogress"), client=client).completed
    assert notion.calls == []

    class BrokenAPI(FakeAPI):
        def call(self, function, **params):
            raise moodle_api.APIError("Moodle API を読めません")

    assert submissions.sync(api=BrokenAPI(), client=client).errors == ["Moodle API を読めません"]
    assert notion.calls == []


def test_manual_and_already_submitted_rows_are_not_checked():
    manual = _row(REPORT, "manual")
    manual["properties"].pop("Moodle ID")
    done = quiz_row()
    done["properties"]["Status"]["status"]["name"] = SUBMITTED
    client, notion = client_for(manual, done)
    api = FakeAPI()
    assert submissions.sync(api=api, client=client).checked == 0
    assert api.calls == [] and notion.calls == []


def test_duplicate_moodle_uid_blocks_writes():
    first, second = quiz_row(), quiz_row()
    second["id"] = "duplicate"
    client, notion = client_for(first, second)
    with pytest.raises(notion_sync.SyncError, match="重複"):
        submissions.sync(api=FakeAPI(), client=client)
    assert notion.calls == []


def test_batch_cursor_reaches_later_unfinished_rows(monkeypatch):
    monkeypatch.setattr(submissions, "MAX_CHECKS", 1)
    first, second = quiz_row(), quiz_row()
    second["id"] = "row-2"
    second["properties"]["Moodle ID"] = {"rich_text": [{"plain_text": "124@moodle.example/moodle"}]}
    client, _ = client_for(first, second)
    result = submissions.sync(api=FakeAPI("inprogress"), client=client)
    assert result.next_id == "row-1" and result.checked == 1
    result = submissions.sync(api=FakeAPI(), client=client, after=result.next_id)
    assert result.next_id == "" and result.completed == ["Short test 1"]


async def test_agent_submission_sync_records_cursor_and_calendar_refresh(config, store, monkeypatch):
    from test_a2a_run import _Updater

    from kei_agent_modules.course.agent import Executor

    def check(**kwargs):
        assert kwargs == {"after": "previous"}
        return submissions.Result(completed=["小テスト"], checked=1, next_id="next")

    monkeypatch.setattr(submissions, "sync", check)
    executor = Executor(config, store)
    executor.agent = "course"
    executor.records.put("submissions", "cursor", {"after": "previous"})
    updater = _Updater()
    await executor.handle(updater, {"skill": "sync-submissions"}, "")
    assert updater.envelope()["data"]["completed"] == ["小テスト"]
    assert executor.records.get("submissions", "cursor") == {"after": "next"}
    assert executor.records.get("calendar", "dirty")["dirty"] is True


def test_one_inaccessible_activity_does_not_block_another_quiz():
    first, second = quiz_row(), quiz_row()
    second["id"] = "row-2"
    second["properties"]["Moodle ID"] = {"rich_text": [{"plain_text": "124@moodle.example/moodle"}]}
    client, notion = client_for(first, second)

    class PartialAPI(FakeAPI):
        def completion(self, url, uid):
            if uid.startswith("123@"):
                raise moodle_api.APIError("この活動を読めません")
            return super().completion(url, uid)

    result = submissions.sync(api=PartialAPI(), client=client)
    assert result.completed == ["Short test 1"] and result.errors == ["この活動を読めません"]
    assert len(notion.calls) == 1 and notion.calls[0][1] == "/pages/row-2"


def test_date_only_deadline_is_visible_through_that_day():
    from kei_agent_modules.course.agent import due_data

    now = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=9)))
    row = {"id": "manual", "title": "時刻未指定", "due": "2026-10-02", "status": NOT_STARTED}
    data = due_data([row], 14, now=now)
    assert len(data["items"]) == 1
    at = datetime.fromisoformat(data["items"][0]["at"])
    assert at.date() == now.date() and at.hour == 23


async def test_partial_notion_failure_still_records_success_and_calendar_refresh(config, store, monkeypatch):
    from test_a2a_run import _Updater

    from kei_agent_a2a.api import NotionError
    from kei_agent_modules.course.agent import Executor

    first, second = quiz_row(), quiz_row()
    second["id"] = "row-2"
    second["properties"]["Moodle ID"] = {"rich_text": [{"plain_text": "124@moodle.example/moodle"}]}

    class PartialNotion(MutableNotion):
        def request(self, method, path, body=None):
            if path == "/pages/row-2":
                raise NotionError("private-response")
            return super().request(method, path, body)

    notion = PartialNotion(assignments=[first, second])
    client = notion_sync.CourseNotion(notion, STATE)
    check = submissions.sync
    monkeypatch.setattr(submissions, "sync", lambda **kwargs: check(api=FakeAPI(), client=client, **kwargs))
    executor = Executor(config, store)
    executor.agent = "course"
    updater = _Updater()
    await executor.handle(updater, {"skill": "sync-submissions"}, "")
    data = updater.envelope()["data"]
    assert data["completed"] == ["Short test 1"] and len(data["errors"]) == 1
    assert "private-response" not in json.dumps(data)
    assert executor.records.get("calendar", "dirty")["dirty"] is True
    assert second["properties"]["Status"]["status"]["name"] == IN_PROGRESS
