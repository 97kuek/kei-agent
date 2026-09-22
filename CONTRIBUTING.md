# コントリビュートの手順

Kei Agent は、Slack で頼んだ作業を Mac 上のエージェントが進めて、同じスレッドに結果を返す Bot です。
仕組みは `docs/design.md`、使い方は `docs/using.md`、入れ方は `deploy/README.md` にあります。

## 変更する前に

- 設計の判断を変えるときは、先に `docs/design.md` の該当する表を直し、理由も書く
- エージェントを増やす・直すときは `docs/agents.md` の決まりに合わせる（名前、返事の封筒、柵）
- Notion の構成を変えるときは `docs/notion-layout.md`、`src/kei_agent/notion.py`（作る側）、`src/kei_agent/notion_store.py`（読み書きする側）を合わせて直す
- 大きな変更（Slack App の権限、sandbox の設定、柵の置き方）は、Issue で相談してから始める
- 使っていて気づいた要望は、Slack の `#00_kei-agent` で `@Kei Agent` をつけて書くと `~/kei-agent/overview/backlog.md` に記録される（研究データと一緒に非公開でバックアップされる）

## 開発の準備

```zsh
brew install pueue && brew services start pueue
uv sync
uv run --group work pytest
```

Slack や Notion につないで動かすときは、`deploy/README.md` の手順で秘密情報のファイルを用意する。

## 確認すること

- `uv run --group work pytest` がすべて通る（プルリクエストと `main` への push では、GitHub Actions でも実行される）
- `uvx ruff check .` が通る（設定は `pyproject.toml` の `[tool.ruff]`）
- Slack を通る動きを変えたときは、手元で `uv run kei-agent` を起動し、テーマのチャンネルで実際に頼んで確かめる
- 定期処理を変えたときは、`uv run kei-agent-schedule <night|literature|daily|review|maintenance>` で1回動かして確かめる（`--record` を付けなければ本番の実行に影響しない）
- `claude -p` の権限や sandbox を変えたときは、テーマのディレクトリの外に書き込めないことを確かめる
- Notion を読み書きする処理を変えたときは、本物の研究ホームで Task やノートを作って確かめ、確認用のページはゴミ箱に移す

## 書き方

- コメント、ログ、Slack への投稿、ドキュメントは日本語で書く
- `plugin/skills/*/scripts/` のスクリプトは sandbox の中で動くので、標準ライブラリだけで書く
- 設定は `config.toml` に、トークンなどの秘密情報は環境変数に置く。秘密情報をコード、コミット、ログ、Issue に含めない

## コミットメッセージ

1行目に、何をするかを日本語で短く書く。文末は「〜する」の形にし、句点は付けない。
必要なら1行空けて、理由と主な変更を書く。

```text
決まった時刻に論文の新着、Daily、振り返り、夜間の Task を動かす

- 07:00 先行研究の新着、08:00 Daily、21:00 振り返りの材料を投稿する
- 🌙 をつけたメッセージを夜間（00:00）の Task として順に実行する
```

- 1つのコミットには1つの目的だけを入れる
- `feat:` などの接頭辞は付けない
- AI の共同作成者の行（`Co-Authored-By:`）や、生成ツールの署名を入れない。プルリクエストの説明も同じ

## ブランチとプルリクエスト

- `main` は常に動く状態にする。launchd の Kei Agent は `main` を動かしている
- 作業はブランチで行い、プルリクエストで `main` に入れる
- プルリクエストには、何を変えたか、どう確かめたかを書く
