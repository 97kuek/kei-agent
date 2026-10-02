"""1回分の依頼。Slack のメッセージ、ジョブの完了、声、夜間の Task のどれからも作る。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Request:
    channel: str
    channel_name: str
    thread_ts: str
    # リアクションをつける元のメッセージ。ジョブ完了で再開するときは None
    message_ts: str | None
    text: str
    # message（依頼者の投稿） / job / voice / night / handoff
    trigger: str = "message"
    files: list[dict] = field(default_factory=list)
    # これ以降に更新された outputs/ のファイルも添付する（ジョブが作ったファイル用）
    outputs_since: float | None = None
    # 終わったあと、依頼者の返事待ちとして扱う（失敗したジョブのあとなど）
    awaiting_after: bool = False
    # 保存した添付の場所（作業場の中）。上限や再起動のあとのやり直しの回にも、AI に渡す
    saved_files: list[str] = field(default_factory=list)
    # 上限や再起動で止まって、あとでやり直した回（依頼者は画面を見ていないので、終わったら知らせる）
    retried: bool = False

    def to_payload(self) -> dict:
        """あとでやり直すために保存する形（Slack の添付の情報は含めず、保存した場所だけ残す）。"""
        return {
            "channel": self.channel, "channel_name": self.channel_name, "thread_ts": self.thread_ts,
            "message_ts": self.message_ts, "text": self.text, "trigger": self.trigger,
            "outputs_since": self.outputs_since, "awaiting_after": self.awaiting_after,
            "saved_files": list(self.saved_files), "retried": self.retried,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> Request:
        # saved_files と retried は、あとから足したもの（前からある控えには無い）
        return cls(**{k: payload[k] for k in (
            "channel", "channel_name", "thread_ts", "message_ts", "text", "trigger", "outputs_since", "awaiting_after")},
            saved_files=list(payload.get("saved_files") or []), retried=bool(payload.get("retried")))
