# Ezra

- Slack で研究の作業を頼むと、自分の Mac のClaude Codeが作業し、経過と結果を同じスレッドに返す研究室のアシスタント

![Ezra の構成](docs/architecture.svg)

##　利用方法

- 研究テーマごとにチャンネルを作り、Ezra を招待する（`#vlm-counting` なら `~/research/vlm-counting/` と Notion のテーマができる）
- テーマのチャンネルで `@Ezra 〜して` と頼む。スレッド内の続きはメンションなしでよい
- 添付したファイルは `inputs/` に保存され、`outputs/` に新しくできたファイルはスレッドに添付される
- 作業中は、返事の中に作業の手順が1行ずつ並ぶ。ジョブが走っている間は「処理中」の表示が残る
- 依頼のメッセージには、受け取ったら 👀、答え終わったら ✅（止まったら ⚠️）がつく
- 長い処理は Ezra がジョブにし、終わると同じスレッドで会話を再開して報告する
- Ezra がつながらない接続先に当たると、スレッドに [許可する] [断る] のボタンを出す。許可はそのテーマだけに効き、押すと作業が再開する
- Slack で Ezra を開いた「ホーム」タブで、テーマごとの接続先と、決まった時刻の処理の時刻・オンオフを変えられる（再起動は要らない）
- 自分のメッセージに 🌙 をつけると、Notion に「今夜やる」の Task ができる。Notion で直接「今夜やる」にした Task も含めて、夜間（01:30）に実行し、結果を Slack と Notion に返す
- 決まった時刻に、先行研究の新着（07:00、テーマのチャンネル）、Daily（08:00）、振り返りの材料（21:00）が届く（`#research-overview` と Notion のノート）
- Ezra への要望は `#research-ezra` に `@Ezra` をつけて書く。案に同意すると Ezra が自分のコードを直し、差分を見せてから取り込み、GitHub に push して、作業が終わったタイミングで新しい版に入れ替わる
- 返事待ちのまま24時間たったスレッドには、Ezra が一度だけ声をかける
- Claude の契約の上限に達したときは、明ける時刻を伝えて、明けてから自動でやり直す（決まった時刻の処理も同じ）

![Ezra の1日](docs/schedule.svg)

## ドキュメント

| 内容 | 場所 |
|---|---|
| セットアップ（Slack App、秘密情報、常時起動） | `deploy/README.md` |
| Codex App 側の使い方、依頼文のテンプレート | `docs/codex.md` |
| 設計とフェーズ | `docs/plan.md` |
| Notion の研究ホームの構成 | `docs/notion-layout.md` |
| 構成図（`docs/architecture.svg`）と1日の流れ（`docs/schedule.svg`） | `docs/` |
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
| `src/ezra/timelog.py` | 研究時間の記録（人は Toggl、Ezra は `runs`）と、週ごとの材料の書き出し |
| `src/ezra/store.py` | SQLite（スレッドとセッション、ジョブ、夜間 Task、定期処理、実行時間） |
| `plugin/` | `claude -p` に読み込ませる skill（`ezra:job`、`ezra:literature`） |
| `prompts/system.md` | `claude -p` に足すシステムプロンプト |

## ライセンス

MIT（`LICENSE`）
