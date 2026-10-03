# 担当（エージェント）

- ローカル AI の担当は研究と仕事。大学は AI を持たない Moodle 同期、知識は Dot のクラウド処理
- Mac の担当は本体から A2A で頼まれ、答えを返す。担当は Slack に投稿しない（Slack に出すのは Dot）
- Dot からは、MCP の `run` に作業場の名前を渡すと、その作業場を受け持つ担当に届く
- 声も自分のプロセスを持つが、AI の実行役は持たない（[architecture.md](architecture.md#声)）

## 一覧

| 領域 | 処理する場所・ポート | チャンネル | 詳しく |
|---|---|---|---|
| 研究 | Mac の AI・8788 | `#1-<テーマ>`（ほかのどれでもないチャンネル） | [research-agent.md](agents/research-agent.md) |
| 大学 | 質問は Dot。Moodle の機械同期は Mac・8787 | `#2-course` | [course-agent.md](agents/course-agent.md) |
| 仕事 | Mac の会社 Claude・8789。Outlook は Dot | `#3-work`・`#work-<名前>` | [work-agent.md](agents/work-agent.md) |
| 知識 | Dot（Mac のプロセスなし） | `#4-knowledge` | [knowledge-agent.md](agents/knowledge-agent.md) |

- 大学の Notion・Box の質問は Dot が接続を直接使う。Mac は起動・復帰時と30分ごとに Moodle の ICS、API 設定時は10分ごとに提出・受験終了を Notion へ同期する
- 知識の検索・記事要約・Notion 保存・毎朝の選定は Dot が行う。Mac の `knowledge` は古い記事の `save_reading` 互換だけを残す
- 通話で受けた依頼と結果も、Dot が対応する Slack チャンネルに記録する

## 触れる範囲（制限の表）

ローカル AI の担当は、作業場の読み書きとコマンドを使える。Web・通信・Notion・連携の範囲は、担当の制限の表で決まる。Dot のクラウド接続は、この表とは別の認証と権限を使う。

| 担当 | 読むデータ | Web・コマンドの通信 | Notion | アカウントの連携 |
|---|---|---|---|---|
| 研究 | 自分 | ○ | 研究ホームを読み書き | なし |
| 仕事 | 会社 | ×（外へ出ない） | なし | Outlook・Teams・SharePoint（現在は会社の Claude、読むだけ。Outlook は通常 Dot が直接読む） |

- 表は各モジュールの `module.toml` の `[actor]`（`data`・`notion`・`connectors`）から作り、実行のときにコアが守らせる
- 守らせ方は [architecture.md](architecture.md#ai-の動かし方)

## どの担当にも共通

以下は、AI を持つ研究・仕事のローカル担当に共通。大学の機械同期は定型の A2A だけを扱う。

| 項目 | 中身 |
|---|---|
| 頼み方 | 決まった仕事（スキル）か、自由な質問 `ask`。スキルの一覧は名刺（`/.well-known/agent-card.json`）に載る |
| AI | 担当ごとに Claude か Codex を選ぶ（`agents.csv` の `engine`）。選ぶまで動かない |
| 深さ（用途） | Dot が MCP の `run` の `weight` または `use_case` を選ぶ。使える用途は `workspaces` に載る |
| 会話 | 1スレッド = 1会話。Dot からは `run` の `conversation`（Slack のスレッドの番号）ごとに1会話。AI と指示書の版が同じ間は続け、変われば始め直す |
| 上限 | MCP の実行中に利用上限に当たったら `failed` で理由を返す。上限中の `run` は受け付けない。Dot が案内し、利用できる AI の選択や再実行を行う |
| 様子の確認 | 「進捗は？」など様子を聞かれただけの回は、読むだけで動かす（作業は始まらない） |
| ログイン切れ | Dot への答えにそう書き、`#0-kei-agent` への知らせ（Dot が出す）に入り直し方を1回だけ書く |
| 持たないもの | Slack への投稿、スレッドと会話の対応、ほかの担当の鍵、ほかの担当への連絡 |

`ask` の依頼と、全部の担当に共通の返事の形:

```json
{"prompt": "…", "session_id": "…", "channel": "C…", "thread_ts": "…", "use_case": "…", "provider": "claude"}
{"ok": true, "text": "人が読む文", "data": {}, "limit_reset_at": null, "cost_usd": 0.02}
```

## アカウントとログ

- 研究は個人、仕事は会社の AI アカウントを使う。仕事の現在の設定は Claude。大学の機械同期は Moodle の秘密情報だけを使い、大学・知識のローカル AI は設定しない。設定は [deploy/README.md](../deploy/README.md#担当ごとの秘密情報)
- ログ: `~/Library/Logs/kei-agent/<名前>-launchd.log`
- 動いているかと版: `curl -s http://127.0.0.1:<ポート>/.well-known/agent-card.json`、`uv run kei-agent doctor`
- 担当を足すときは [modules.md](modules.md)（`kei-agent module new <名前> --ai --process`）
