# 開発の手順

- Kei Agent は個人用に作ったアシスタント。自分用に作り替えるのは自由
- アイデアは ueki.keitaro@gmail.com まで

## 準備

```zsh
brew install pueue && brew services start pueue
uv sync --all-groups
uv run python -m pytest
```

- Slack や Notion につないで動かすときは、[deploy/README.md](deploy/README.md) の秘密情報を用意する

## 確かめること

| 変えたもの | 確かめ方 |
|---|---|
| どれでも | `uv run python -m pytest` と `uvx ruff check .`（GitHub Actions も同じものを回す） |
| Slack を通る動き | 手元で `uv run kei-agent` を起動し、テーマのチャンネルで実際に頼む |
| 定期処理 | `uv run kei-agent-schedule <名前>` で1回動かす（`--record` なし） |
| sandbox や権限 | 作業場の外に書き込めないこと |
| Notion の読み書き | 本物のホームで作って確かめ、確認用のページはゴミ箱に移す |

## モジュールを足すとき

- 機能はどれもモジュール（`modules/<名前>/`）にする。`src/` と `pyproject.toml` には手を入れない
- 手順と書き方は [docs/modules.md](docs/modules.md)

1. `uv run kei-agent module new <名前> --builtin` でひな形を作る（AI は `--ai`、担当プロセスは `--process`）
2. `module.toml`・`module.py`（と指示書・`agent.py`）を書く
3. テストを `tests/` に書き、`uv run --group agents kei-agent module test <名前>` で通す
4. 要る秘密情報は `[secrets]` に名前と説明だけを書く
5. `config.example.toml` の `modules` に足す。設定を持つなら `[<名前>]` の例も書く
6. スラッシュコマンドを足したら、`uv run kei-agent manifest > slack/manifest.yaml` で書き直す
7. README の「何ができるか」の表に1行足す（テストが `modules/` と照らし合わせる）

## 変えるときの決まり

- 仕組みを変えたら、同じ変更で文書も直す（使い方は `docs/using.md`、担当は `docs/agents/`、仕組みは `docs/architecture.md`、入れ方は `deploy/README.md`）
- 文書には、いまどうなっているかだけを書く。経緯は Git の履歴に残す
- 文書は箇条書きと表を中心にし、同じことを2か所に書かない（片方からはリンクする）
- 図は `docs/images/diagrams.py` を書き換えて `uv run python docs/images/diagrams.py` で作り直す
- Notion の DB や項目の名前を変えたら、`notion.py`・`notion_store.py`・`notion_hub.py`・`modules/course/notion_setup.py` も合わせる
- モデル名は `src/kei_agent/execution/model_policy.py` と `module.toml` の `[use_cases]` にだけ書く
- Slack の権限・sandbox・柵（`guard.py`・`config.example.toml`・`deploy/`）の大きな変更は、先に Issue で相談する

## 書き方

- コメント・ログ・Slack への投稿・文書は日本語
- 秘密情報をコード・コミット・ログ・Issue に含めない

## コミットとプルリクエスト

- 1行目は `feat:`・`fix:`・`docs:`・`ci:` などを頭に付け、何をするかを英語で短く書く。必要なら1行空けて理由と主な変更
- 1つのコミットには1つの目的
- `main` は常に動く状態に保つ（launchd の Kei Agent は `main` を動かしている）。作業はブランチで行う
- プルリクエストには、何を変えたかと、どう確かめたかを書く
