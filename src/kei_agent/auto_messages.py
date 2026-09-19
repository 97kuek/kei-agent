"""Kei Agent から Claude に渡す自動メッセージ。依頼者の言葉ではなく、会話を再開・復元するときに使う。"""

from __future__ import annotations

from kei_agent.jobs import log_tail
from kei_agent.slack_text import DONE_PREFIX, FAILED_PREFIX, PROGRESS_PREFIX, clean_text, format_duration, message_text
from kei_agent.store import Job

HEADER = "[Kei Agent からの自動メッセージ]"

_JOB_STATUS = {"succeeded": "成功", "failed": "失敗", "cancelled": "取り消し"}


def job_status_label(status: str) -> str:
    return _JOB_STATUS.get(status, status)


def job_resume_prompt(job: Job) -> str:
    elapsed = ""
    if job.started_at and job.finished_at:
        elapsed = f"（実行時間 {format_duration(job.finished_at - job.started_at)}）"
    detail = f"\n詳細: {job.detail}" if job.detail else ""
    tail = log_tail(job)
    tail_block = f"\n\nログの末尾:\n```\n{tail}\n```" if tail else ""
    return (
        f"{HEADER} ジョブ {job.id}「{job.name}」が終わりました。"
        f"結果: {job_status_label(job.status)}{elapsed}{detail}\n"
        f"実行したもの: {job.command}\nログ: logs/job-{job.id}.log{tail_block}\n\n"
        "kei-agent:job skill の「ジョブが終わって会話が再開されたとき」に沿って、結果を確認して報告してください。"
    )


def thread_history(messages: list[dict], bot_user_id: str, exclude_ts: str | None = None) -> str:
    """スレッドのやり取りを「誰: 何」の形に並べる。作業中や完了の印だけの投稿は省く。"""
    lines = []
    for m in messages:
        text = message_text(m)
        if m.get("ts") == exclude_ts or text.startswith((PROGRESS_PREFIX, DONE_PREFIX, FAILED_PREFIX)):
            continue
        who = "Kei Agent" if m.get("user") == bot_user_id or m.get("bot_id") else "依頼者"
        lines.append(f"{who}: {clean_text(text)}")
    return "\n\n".join(lines)


def history_prompt(messages: list[dict], bot_user_id: str, new_text: str, exclude_ts: str | None,
                   stalled: str | None = None) -> str:
    """セッションが失われたときや、直前の依頼がエラーで止まったときに、スレッドの履歴から文脈を復元するためのプロンプト。"""
    history = thread_history(messages, bot_user_id, exclude_ts)
    if stalled:
        why = "直前の依頼はエラーや上限で止まり、会話に記録が残っていない可能性があるため"
        note = f"止まった依頼（まだ終わっていない）:\n{stalled}\n\n"
    else:
        why = "以前の会話セッションが見つからないため"
        note = ""
    return (
        f"{HEADER} このスレッドの{why}、"
        "Slack のスレッドの履歴から文脈を復元します。\n\n"
        f"<thread_history>\n{history}\n</thread_history>\n\n{note}"
        f"続きの依頼:\n{new_text}"
    )


def rules_update_prompt(rules: str) -> str:
    """会話の途中で prompts/system.md が変わったときに、依頼の前に付ける文。"""
    return (f"{HEADER} Kei Agent としての振る舞いの決まりが更新されました。"
            "この会話を始めたときの決まりより、こちらを優先してください。\n\n"
            f"<rules>\n{rules.strip()}\n</rules>\n\n---\n\n")


def domain_resume_prompt(decisions) -> str:
    """接続先の申し出に依頼者が答えたあと、会話を再開するときに渡す文。"""
    lines = [f"{HEADER} 接続先の申し出に、依頼者が答えました。"]
    for d in decisions:
        if d["status"] == "allowed":
            lines.append(f"- `{d['domain']}`: 許可されました。次のコマンドからつながります")
        else:
            lines.append(f"- `{d['domain']}`: 断られました。このテーマでは使えません")
    lines.append("止まっていた作業を続けてください。断られたものがあれば、別の入手先を探すか、"
                 "ここで止めてどうするかを報告してください。")
    return "\n".join(lines)


HANDOFF_MEMO_PROMPT = (
    f"{HEADER} 依頼者が、このスレッドを区切って新しいスレッドで続けることにしました。"
    "新しいスレッドの最初に置く引き継ぎメモを書いてください。道具は使わず、この会話で分かっていることだけで書きます。\n\n"
    "- 1行目は、新しいスレッドで話すことの題だけを、30字以内で書く（記号や「題:」は付けない）\n"
    "- 2行目からは、`**目的**` `**決まったこと**` `**分かったこと**` `**次にやること**` の順に、それぞれ箇条書きで3行まで。"
    "結果のファイルがあれば、ファイル名を添える\n"
    "- 専門用語は、そのまま使わずに短く言い換える\n"
    "- `❓ 確認:` や `🧵 区切り:` などの合図の行は書かない"
)


def handoff_start_prompt(memo: str, previous_log: str) -> str:
    """区切って立てた新しいスレッドの、最初の回に付ける文。"""
    return (
        f"{HEADER} このスレッドは、前のスレッドを区切って引き継いだものです。前のスレッドの要点:\n\n"
        f"<handoff>\n{memo.strip()}\n</handoff>\n\n"
        f"細かい経緯が要るときは `{previous_log}` を読んでください。\n\n---\n\n"
    )


def interrupted_prompt(text: str) -> str:
    """再起動で途中で止まった依頼を、やり直すときに付ける文。"""
    return (f"{HEADER} この依頼は、Kei Agent の再起動で途中で止まりました。"
            "前回の作業の続きがファイルに残っていることがあるので、まず今の状態を確かめてから、"
            "足りないところだけをやり直してください。\n\n"
            f"止まった依頼:\n{text}")
