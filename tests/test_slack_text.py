import pytest

from kei_agent.conversation.slack_text import is_status_inquiry

INQUIRIES = ["今どんな感じ？", "終わった?", "進捗どう?", "できました?", "まだ止まってる?", "状況は？", "どうなってる？",
             "どこまで進んだ？", "状況を教えて", "様子はどう？", "直った？"]
NEW_REQUESTS = [
    "図を作って", "", "集計して",
    "条件Aで回して、終わったらBもお願い",  # 長い文なので新しい依頼として扱う
    # 頼み方で終わる文は、様子を聞いているのではない
    "進捗表示を直して", "様子を見て", "できたら送って", "直してくれる？", "直ったら取り込んで",
]


@pytest.mark.parametrize(("text", "inquiry"), [(t, True) for t in INQUIRIES] + [(t, False) for t in NEW_REQUESTS])
def test_short_progress_checks_are_told_apart_from_new_requests(text, inquiry):
    assert is_status_inquiry(text) is inquiry
