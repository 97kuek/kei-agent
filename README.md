# Research Support Assistant「Ezra」

Slack で研究の作業を頼むと、自分の Mac の Claude Code（`claude -p`）が作業し、経過と結果を同じスレッドに返す研究室の助手。
研究支援アシスタント「Doctor AI」の構想資料をもとに作っている。設計の判断は `docs/plan.md`。

```
Codex App（考える・音声で議論）
   │ 依頼文
   ▼
Slack  ──Socket Mode──▶  Ezra（このリポジトリ）
   ▲                       ├─ claude -p（sandbox、テーマのディレクトリの中だけ）
   │ 経過・結果・図         ├─ pueue（数分以上かかるジョブ）
   └───────────────────────┘
                           ~/research/<theme>/  （CLAUDE.md, inputs, outputs, logs, papers）
```

## 使い方

- 研究テーマごとにチャンネルを作り、Ezra を招待する（`#vlm-counting` なら `~/research/vlm-counting/` と Notion のテーマができる）
- テーマのチャンネルで `@Ezra 〜して` と頼む。スレッド内の続きはメンションなしでよい
- 添付したファイルは `inputs/` に保存され、`outputs/` に新しくできたファイルはスレッドに添付される
- 長い処理は Ezra がジョブにし、終わると同じスレッドで会話を再開して報告する
- 自分のメッセージに 🌙 をつけると、Notion に「今夜やる」の Task ができる。Notion で直接「今夜やる」にした Task も含めて、夜間（01:30）に実行し、結果を Slack と Notion に返す
- 決まった時刻に、先行研究の新着（07:00、テーマのチャンネル）、Daily（08:00）、振り返りの材料（21:00）が届く（`#research-overview` と Notion のノート）
- Ezra への要望は `#research-ezra` に `@Ezra` をつけて書く
- 返事待ちのまま24時間たったスレッドには、Ezra が一度だけ声をかける

## ドキュメント

| 内容 | 場所 |
|---|---|
| セットアップ（Slack App、秘密情報、常時起動） | `deploy/README.md` |
| Codex App 側の使い方、依頼文のテンプレート | `docs/codex.md` |
| 設計とフェーズ | `docs/plan.md` |
| Notion の研究ホームの構成 | `docs/notion-layout.md` |
| 変更の手順、コミットメッセージの書き方 | `CONTRIBUTING.md` |

## 保守

- 毎晩 22:00 に、古いファイルを整理し、`~/research/` を非公開の GitHub リポジトリにバックアップする
- ログは 5MB ごとに回す（`deploy/README.md` の8章）

## 開発

```zsh
uv sync
uv run pytest
```

| 部品 | 役割 |
|---|---|
| `src/ezra/app.py` | Slack Bolt（Socket Mode）の受け口と起動 |
| `src/ezra/assistant.py` | 依頼の受け付け、経過、結果の返信、ジョブ完了時の再開 |
| `src/ezra/runner.py` | `claude -p` の起動、sandbox と権限の設定、stream-json の読み取り |
| `src/ezra/jobs.py` | ジョブの依頼の検証、pueue への投入、状態の追跡 |
| `src/ezra/themes.py` | チャンネル、テーマ、作業用ディレクトリの対応 |
| `src/ezra/schedule.py` | 決まった時刻の処理（先行研究、Daily、振り返り、夜間 Task、声かけ） |
| `src/ezra/notion.py` | Notion の API の接続と、研究ホームを作るコマンド |
| `src/ezra/notion_store.py` | Task とノートの読み書き（夜間の Task、Daily、振り返り） |
| `src/ezra/maintenance.py` | 毎晩の保守（古いファイルの整理、研究データのバックアップ） |
| `src/ezra/store.py` | SQLite（スレッドとセッション、ジョブ、夜間 Task、定期処理、実行時間） |
| `plugin/` | `claude -p` に読み込ませる skill（`ezra:job`、`ezra:literature`） |
| `prompts/system.md` | `claude -p` に足すシステムプロンプト |

## ライセンス

MIT（`LICENSE`）
