import pytest

from kei_agent.review_output import ReviewOutputError, review_footer, validate_review_reply


VALID = """*今日の成果*
なし

*未完了タスク*
なし

夜間に実行したいタスクはありますか？"""


def test_accepts_exact_review_contract():
    assert validate_review_reply(VALID) == VALID


def test_rejects_progress_narration_before_contract():
    with pytest.raises(ReviewOutputError, match="指定形式"):
        validate_review_reply("まず材料を確認します。\n" + VALID)


def test_rejects_extra_footer_after_contract():
    with pytest.raises(ReviewOutputError, match="指定形式"):
        validate_review_reply(VALID + "\nCodex App を開いてください")


@pytest.mark.parametrize("text", [
    "*今日の成果*\n\n\n*未完了タスク*\nなし\n\n夜間に実行したいタスクはありますか？",
    "# 今日\n" + VALID,
    "*今日の成果*\n/Users/keitaro/private\n\n*未完了タスク*\nなし\n\n夜間に実行したいタスクはありますか？",
])
def test_rejects_empty_or_internal_review_content(text):
    with pytest.raises(ReviewOutputError, match="指定形式"):
        validate_review_reply(text)


def test_review_footer_never_mentions_codex_app_or_path():
    footer = review_footer("https://notion.so/review", "Retro & Planning 9/23（水）")
    assert "Codex App" not in footer and "/Users/" not in footer
    assert "このスレッド" in footer and "https://notion.so/review" in footer


def test_review_footer_without_notion_uses_thread_only():
    assert review_footer(None, "Retro") == "振り返りの結論があれば、このスレッドに書いてね。明日の Daily に反映するよ。"
