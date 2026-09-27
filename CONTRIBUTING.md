# 開発の手順

Kei Agent は個人用に作ったアシスタントです。自分用に作り替えるのは自由です。アイデアは ueki.keitaro@gmail.com までどうぞ。

仕組みは [`docs/architecture.md`](docs/architecture.md)、入れ方は [`deploy/README.md`](deploy/README.md)。

## 準備

```zsh
brew install pueue && brew services start pueue
uv sync --all-groups
uv run python -m pytest
```

Slack や Notion につないで動かすときは、`deploy/README.md` の秘密情報のファイルを用意する。

## 確かめること

- `uv run python -m pytest` と `uvx ruff check .` が通る（プルリクエストと `main` への push で GitHub Actions も同じものを回す）
- Slack を通る動きを変えたら、手元で `uv run kei-agent` を起動し、テーマのチャンネルで実際に頼む
- 定期処理を変えたら `uv run kei-agent-schedule <名前>` で1回動かす（`--record` なし）
- sandbox や権限を変えたら、作業場の外に書き込めないことを確かめる
- Notion の読み書きを変えたら、本物のホームで作って確かめ、確認用のページはゴミ箱に移す

## 変えるときの決まり

- 仕組みを変えたら `docs/architecture.md` の該当する節も同じ変更で直す。使い方が変わるなら `docs/using.md`、入れ方なら `deploy/README.md`
- ドキュメントには「いまどうなっているか」だけを書く。経緯は Git の履歴に残す
- Notion の DB やプロパティの名前を変えたら、`src/kei_agent/notion.py`・`notion_store.py`・`notion_hub.py`（大学は `modules/course/notion_setup.py`）も合わせる
- モデル名は `src/kei_agent/model_policy.py` にだけ書く。skill や prompt に埋め込まない
- Slack の権限、sandbox、柵（`guard.py`、`config.example.toml`、`deploy/`）の大きな変更は、先に Issue で相談する

## モジュールを足すとき

機能はどれもモジュール（`modules/<名前>/`）にする。`src/` と `pyproject.toml` には手を入れない。手順と書き方は [`docs/modules.md`](docs/modules.md)。

1. `uv run kei-agent module new <名前> --builtin` でひな形を作る（AI の実行役は `--ai`、担当プロセスは `--process`）
2. `module.toml`・`module.py`（と指示書・`agent.py`）を書く。読み込んでよい Kei Agent の部品は窓口（`module.py` は `kei_agent.api`、そのほかは `kei_agent_a2a.api`）だけ
3. テストはモジュールの `tests/` に書き、`uv run --group agents kei-agent module test <名前>` で通す（`kei_agent.testing`。本物の Slack・AI・秘密情報には触れない）
4. 要る秘密情報は `module.toml` の `[secrets]` に名前と説明だけを書く。値はどこにも書かない
5. `config.example.toml` の `modules` に足し、設定（`[settings]`）を持つなら `[<名前>]` の例も書く。スラッシュコマンドを足したら、`slack/manifest.yaml` を `uv run kei-agent manifest` の出力で書き直す
6. 使い方が変わるなら `docs/using.md`、仕組みが変わるなら `docs/architecture.md` も同じ変更で直す

## 書き方

- コメント、ログ、Slack への投稿、ドキュメントは日本語
- 設定は `~/.config/kei-agent/config.toml`（リポジトリには `config.example.toml` だけ）、秘密情報は環境変数。秘密情報をコード、コミット、ログ、Issue に含めない

## コミットとプルリクエスト

- 1行目に何をするかを日本語で短く書く（「〜する」で終え、句点なし）。必要なら1行空けて理由と主な変更
- 1つのコミットには1つの目的
- `main` は常に動く状態に保つ（launchd の Kei Agent は `main` を動かしている）。作業はブランチで行い、プルリクエストで入れる
- プルリクエストには、何を変えたかと、どう確かめたかを書く
