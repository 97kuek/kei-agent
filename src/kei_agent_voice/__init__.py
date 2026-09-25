"""机の上の音声対話（docs/architecture.md の「声のレイヤ」）。

声は OpenAI Realtime API に任せ、依頼者のことは道具として渡す。
研究の中身を調べるのは Codex、作業するのは Slack の Kei Agent。
Kei Agent 本体とは別のパッケージにしてある（試すのをやめるときに消しやすい）。
"""
