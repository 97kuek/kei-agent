import pytest

from kei_agent.response_output import (
    OutputError,
    finalize_conversation,
    trouble_notice,
    validate_daily,
    validate_structured_response,
)


def test_finalizer_keeps_only_the_marked_user_facing_answer():
    raw = "まず材料を確認します。\n<<kei-agent-final>>\n僕が調べた結果、課題はないよ。\n<<kei-agent-final-end>>"

    assert finalize_conversation(raw) == "僕が調べた結果、課題はないよ。"


def test_finalizer_rejects_missing_marker():
    with pytest.raises(OutputError):
        finalize_conversation("確認します")


def test_finalizer_keeps_workspace_file_names():
    # プロンプトは outputs/ のファイル名を書くよう頼んでいる。これで返答全体を捨てない
    raw = "<<kei-agent-final>>\n図は `outputs/dropfrac_test.png`、要約は reviews/2026-09-24.md に置いたよ\n<<kei-agent-final-end>>"

    assert finalize_conversation(raw) == "図は `outputs/dropfrac_test.png`、要約は reviews/2026-09-24.md に置いたよ"


def test_finalizer_hides_absolute_local_paths_instead_of_dropping_the_answer():
    raw = ("<<kei-agent-final>>\n結果は /Users/kei/research/amr/outputs/fig.png と "
           "~/notes/memo.md、file:///tmp/x.txt にあるよ\n<<kei-agent-final-end>>")

    assert finalize_conversation(raw) == "結果は fig.png と memo.md、x.txt にあるよ"


def test_finalizer_allows_ordinary_words_about_steps_and_tools():
    raw = "<<kei-agent-final>>\nまず締切を確認するといいよ。Claude Code の話なら続けるね\n<<kei-agent-final-end>>"

    assert finalize_conversation(raw) == "まず締切を確認するといいよ。Claude Code の話なら続けるね"


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


def test_trouble_notice_says_what_happened_without_local_paths():
    notice = trouble_notice("共通 Notion ホームを利用できません。/Users/kei/.local/state/kei-agent/hub.json を確認して")

    assert notice == "共通 Notion ホームを利用できません。hub.json を確認して"
    assert len(trouble_notice("あ" * 500)) == 200
    assert trouble_notice("course が返した理由: RuntimeError: secret") == "course が返した理由"
