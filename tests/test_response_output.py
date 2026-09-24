import pytest

from kei_agent.response_output import OutputError, finalize_conversation, validate_daily, validate_structured_response


def test_finalizer_keeps_only_the_marked_user_facing_answer():
    raw = "まず材料を確認します。\n<<kei-agent-final>>\n僕が調べた結果、課題はないよ。\n<<kei-agent-final-end>>"

    assert finalize_conversation(raw) == "僕が調べた結果、課題はないよ。"


def test_finalizer_rejects_missing_marker_and_local_path():
    with pytest.raises(OutputError):
        finalize_conversation("確認します")
    with pytest.raises(OutputError):
        finalize_conversation("<<kei-agent-final>>\nreviews/today.md に保存したよ\n<<kei-agent-final-end>>")


def test_finalizer_allows_a_normal_web_link():
    text = "<<kei-agent-final>>\n資料は https://example.com/notes にあるよ。\n<<kei-agent-final-end>>"

    assert finalize_conversation(text) == "資料は https://example.com/notes にあるよ。"


def test_structured_response_rejects_exception_details_and_local_paths():
    assert validate_structured_response("新しい課題を 2 件取り込んだよ") == "新しい課題を 2 件取り込んだよ"
    with pytest.raises(OutputError):
        validate_structured_response("RuntimeError: /private/secret")


def test_daily_requires_four_bold_sections_in_order():
    valid = """**今日のタスク**
なし

**夜間処理の結果**
なし

**確認待ち・期日・止まっているテーマ・返事待ち**
なし

**今日考えるとよい問い**
1. 何から進める？"""

    assert validate_daily(valid) == valid
    with pytest.raises(OutputError):
        validate_daily("*今日のタスク*\nなし")
