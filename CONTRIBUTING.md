# 開発の手順

- Kei Agent は設定とモジュールで用途を変えられるアシスタント。個人の前提を共通コードに埋め込まない
- アイデアは ueki.keitaro@gmail.com まで

## 準備

```zsh
brew install pueue && brew services start pueue
uv sync --all-groups
uv run python -m pytest
```

- Dot の Slack 接続と MCP の設定は [docs/dots.md](docs/dots.md)。Mac の常駐とローカルの秘密情報の配置は [deploy/README.md](deploy/README.md)

## 確かめること

| 変えたもの | 確かめ方 |
|---|---|
| どれでも | `uv run python -m pytest` と `uvx ruff check .`（GitHub Actions も同じものを回す） |
| MCP・通知を通る動き | 偽物の AI と Outbox で `run`・`status` と変更した道具を確かめる。`notices` の取得だけでは配信済みにならず、配信できた ID を `done` で返すと未配信から外れることを見る。Dot の設定は [docs/dots.md](docs/dots.md) |
| 定期処理 | 偽物の設定と状態で1回動かし、通知と記録を確かめる。`--record` なしでも通知や Notion は更新し得る |
| sandbox や権限 | 作業場の外に書き込めないこと |
| Notion の読み書き | `tests/fakes.py` の偽物を使い、本物のホームや状態に触れない |

## モジュールを足すとき

- 機能はどれもモジュール（`modules/<名前>/`）にする。`src/` と `pyproject.toml` には手を入れない
- 手順と書き方は [docs/modules.md](docs/modules.md)

1. `uv run kei-agent module new <名前> --builtin` でひな形を作る（AI は `--ai`、担当プロセスは `--process`）
2. `api = 2` の `module.toml`・`module.py`（と指示書・`agent.py`）を書く
3. テストを `tests/` に書き、`ModuleKit.head_action` などで窓口を通して確かめる。`uv run --group agents kei-agent module test <名前>` で通す
4. 要る秘密情報は `[secrets]` に名前と説明だけを書く
5. `agents.example.csv` に行を足す。設定の既定は `module.toml` の `[settings]` に書く。保護対象の `config.example.toml` の変更が必要なら人に頼む
6. Dot からの操作は `async head_action(name, params)`、材料は `async head_materials(days)` に書く。操作は MCP の道具と [Dot の指示](docs/dots.md) を合わせる。扱わない操作名には `None` を返す
7. README の「何ができるか」の表に1行足す（テストが `modules/` と照らし合わせる）

## 変えるときの決まり

- モジュールの窓口や宣言の契約を変えたら枠の版を上げ、[docs/modules.md](docs/modules.md) と直し方を合わせる
- 仕組みを変えたら、同じ変更で文書も直す（使い方は `docs/using.md`、担当は `docs/agents/`、仕組みは `docs/architecture.md`、Dot の接続と予定は `docs/dots.md`。保護対象の `deploy/` の変更が必要なら人に頼む）
- 文書には、いまどうなっているかだけを書く。経緯は Git の履歴に残す
- 文書は箇条書きと表を中心にし、同じことを2か所に書かない（片方からはリンクする）
- 図は `docs/images/diagrams.py` を書き換えて `uv run python docs/images/diagrams.py` で作り直す
- Notion の DB や項目の名前を変えたら、`notion.py`・`notion_store.py`・`notion_hub.py`・`modules/course/notion_setup.py` も合わせる
- モデル名は `src/kei_agent/framework/models.py` と `module.toml` の `[use_cases]` にだけ書く
- Slack の権限・sandbox・柵（`guard.py`・`config.example.toml`・`deploy/`）の大きな変更は、先に Issue で相談する

## 書き方

- コメント・ログ・Slack への投稿・文書は日本語
- 秘密情報をコード・コミット・ログ・Issue に含めない

## コミットとプルリクエスト

- 1行目は `feat:`・`fix:`・`docs:`・`ci:` などを頭に付け、何をするかを英語で短く書く。必要なら1行空けて理由と主な変更
- 1つのコミットには1つの目的
- `main` は常に動く状態に保つ（launchd の Kei Agent は `main` を動かしている）。作業はブランチで行う
- プルリクエストには、何を変えたかと、どう確かめたかを書く
