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
- Notion の DB やプロパティの名前を変えたら、`src/kei_agent/notion.py`・`notion_store.py`・`notion_hub.py`（大学は `src/kei_agent_course/notion_setup.py`）も合わせる
- モデル名は `src/kei_agent/model_policy.py` にだけ書く。skill や prompt に埋め込まない
- Slack の権限、sandbox、柵（`guard.py`、`config.toml`、`deploy/`）の大きな変更は、先に Issue で相談する

## エージェントを増やすとき

名前（`<agent>`）をすべての場所でそろえる。

1. `src/kei_agent_<agent>/` に `card.py`（名刺。スキルの ID は kebab-case の動詞-目的語、自由な質問は `ask`）、`executor.py`、`app.py`（`kei_agent_a2a.server.serve` を呼ぶだけ）
2. `pyproject.toml` に `kei-agent-<agent>` のコマンドと `[dependency-groups] <agent> = [{ include-group = "agents" }]`
3. `config.toml` の `[a2a.agents]` に `127.0.0.1` の次のポート、`[agents.<agent>]`、`[channels]`
4. `deploy/run-<agent>.sh`、`deploy/com.kei-agent.<agent>.plist.template`、`deploy/install.sh` の `case`
5. 秘密情報は `~/.config/zsh/local/kei-agent-<agent>.zsh`（600）に分ける
6. 本体の取り次ぎ（`src/kei_agent/<agent>.py`）と、`model_policy.py` の用途と recipe
7. skill を持たせるなら `plugin/<agent>/`（`kei-agent-<agent>` という名前の plugin、`skills/<skill>/SKILL.md`、`hooks/`）を作り、`AGENT_PLUGINS` に足す。スクリプトは標準ライブラリだけで書く
8. テストは本物の A2A サーバーを立てて往復を見る（`tests/test_a2a.py` の型）。外部サービスとモデルは偽物にする

## 書き方

- コメント、ログ、Slack への投稿、ドキュメントは日本語
- 設定は `config.toml`、秘密情報は環境変数。秘密情報をコード、コミット、ログ、Issue に含めない

## コミットとプルリクエスト

- 1行目に何をするかを日本語で短く書く（「〜する」で終え、句点なし）。必要なら1行空けて理由と主な変更
- 1つのコミットには1つの目的
- `main` は常に動く状態に保つ（launchd の Kei Agent は `main` を動かしている）。作業はブランチで行い、プルリクエストで入れる
- プルリクエストには、何を変えたかと、どう確かめたかを書く
