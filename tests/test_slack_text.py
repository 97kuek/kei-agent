import pytest

from kei_agent.slack_text import is_status_inquiry


@pytest.mark.parametrize("text", [
    "今どんな感じ？",
    "終わった?",
    "進捗どう?",
    "できました?",
    "まだ止まってる?",
    "状況は？",
    "どうなってる？",
    "どこまで進んだ？",
    "状況を教えて",
    "様子はどう？",
    "直った？",
])
def test_is_status_inquiry_true_for_short_progress_checks(text):
    assert is_status_inquiry(text)


@pytest.mark.parametrize("text", [
    "図を作って",
    "条件Aで回して、終わったらBもお願い",  # 長い文なので新しい依頼として扱う
    "",
    "集計して",
    "進捗表示を直して",      # 頼み方で終わる文は、様子を聞いているのではない
    "様子を見て",
    "できたら送って",
    "直してくれる？",
    "直ったら取り込んで",
])
def test_is_status_inquiry_false_for_new_requests(text):
    assert not is_status_inquiry(text)
