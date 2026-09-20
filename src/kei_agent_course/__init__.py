"""大学の用事を引き受けるエージェント（A2A サーバー）。

Kei Agent 本体（オーケストレーター）から A2A で頼まれて、Moodle の課題、Notion の授業と課題、
Toggl の実績を扱う。Slack と柵と LLM はオーケストレーターが持ち、ここは道具に徹する（docs/plan.md の16章）。

Kei Agent 本体とは別のパッケージにして、依存（a2a-sdk）をこちらに閉じてある。
"""
