import pytest

from kei_agent.review_output import ReviewOutputError, validate_review_reply

VALID = """**今日の成果**
なし

**未完了タスク**
なし

夜間に実行したいタスクはありますか？"""


def test_accepts_exact_review_contract():
    assert validate_review_reply(VALID) == VALID


def test_allows_a_normal_web_link_in_an_outcome():
    text = VALID.replace("なし", "資料: https://example.com/notes", 1)
    assert validate_review_reply(text) == text


def test_rejects_progress_narration_before_contract():
    with pytest.raises(ReviewOutputError, match="指定形式"):
        validate_review_reply("まず材料を確認します。\n" + VALID)


def test_rejects_extra_footer_after_contract():
    with pytest.raises(ReviewOutputError, match="指定形式"):
        validate_review_reply(VALID + "\nCodex App を開いてください")


@pytest.mark.parametrize("text", [
    "**今日の成果**\n\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "# 今日\n" + VALID,
    "**今日の成果**\n/Users/keitaro/private\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "**今日の成果**\n/tmp/private.md\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "**今日の成果**\nfile:///private/private.md\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "**今日の成果**\n~/.config/private\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "**今日の成果**\nレビューを reviews/2026-09-24.md に保存しました\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "**今日の成果**\nBash で材料を読みました。\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
    "**今日の成果**\nSkill を使って調査中です。\n\n**未完了タスク**\nなし\n\n夜間に実行したいタスクはありますか？",
])
def test_rejects_empty_or_internal_review_content(text):
    with pytest.raises(ReviewOutputError, match="指定形式"):
        validate_review_reply(text)
