# Kei Agent を直すとき

Claude Code・Codex・自己改善の AI が、このリポジトリを直す前に読む。決まりと手順の正本は [CONTRIBUTING.md](CONTRIBUTING.md)。

- 仕組みは [docs/architecture.md](docs/architecture.md)、設計の決めごとは [docs/extensibility.md](docs/extensibility.md)、モジュールの作り方は [docs/modules.md](docs/modules.md)
- 確かめる: `uv run python -m pytest` と `uvx ruff check .`。sandbox の中ではソケットを使うテストが落ちる（外で回す）
- 機能はモジュール（`modules/<名前>/`）に書く。モジュールがコアに触れるのは `kei_agent.api` の窓口だけ
- モジュールのオンオフ・チャンネル・AI は `~/.config/kei-agent/agents.csv`。`config.toml` には書かない
- モデル名は `src/kei_agent/model_policy.py` と `module.toml` の `[use_cases]` にだけ書く
- 柵（`src/kei_agent/guard.py`・`config.example.toml`・`deploy/`）は変えない。要るときは人に頼む
- 本物の状態・秘密情報・launchd に触れない。テストは `tests/fakes.py` の偽物と `write_config` を使う
- コメント・文書・Slack の文は日本語。文書には今の状態だけを書く（経緯は Git の履歴に）
- コミットの1行目は `feat:`・`fix:`・`docs:` などを頭に付けた英語の短い文
