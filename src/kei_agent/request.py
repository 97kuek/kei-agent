"""1回分の依頼。Slack のメッセージ、ジョブの完了、接続先の返事、声、夜間の Task のどれからも作る。"""

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
    # message（依頼者の投稿） / job / domain / voice / night / handoff
    trigger: str = "message"
    files: list[dict] = field(default_factory=list)
    # これ以降に更新された outputs/ のファイルも添付する（ジョブが作ったファイル用）
    outputs_since: float | None = None
    # 終わったあと、依頼者の返事待ちとして扱う（失敗したジョブのあとなど）
    awaiting_after: bool = False

    def to_payload(self) -> dict:
        """あとでやり直すために保存する形（添付は保存済みなので含めない）。"""
        return {
            "channel": self.channel, "channel_name": self.channel_name, "thread_ts": self.thread_ts,
            "message_ts": self.message_ts, "text": self.text, "trigger": self.trigger,
            "outputs_since": self.outputs_since, "awaiting_after": self.awaiting_after,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> Request:
        return cls(**{k: payload[k] for k in (
            "channel", "channel_name", "thread_ts", "message_ts", "text", "trigger", "outputs_since", "awaiting_after")})
