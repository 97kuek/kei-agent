# 担当（エージェント）

- 担当 = AI の実行役を持ち、自分のプロセスで動くモジュール
- 本体から A2A で頼まれ、答えを返す。Slack に投稿するのは本体だけ
- 声も自分のプロセスを持つが、AI の実行役は持たない（[architecture.md](architecture.md#声)）

## 一覧

| 担当 | 番地 | チャンネル | 詳しく |
|---|---|---|---|
| 研究 | 8788 | `#10_<テーマ>`（ほかのどれでもないチャンネル） | [research-agent.md](agents/research-agent.md) |
| 大学 | 8787 | `#20_course` | [course-agent.md](agents/course-agent.md) |
| 仕事 | 8789 | `#30_work` | [work-agent.md](agents/work-agent.md) |
| 知識 | 8792 | `#40_knowledge` | [knowledge-agent.md](agents/knowledge-agent.md) |

## 触れる範囲（制限の表）

| 担当 | ファイル | コマンド | Web | Notion | アカウントの連携 |
|---|---|---|---|---|---|
| 研究 | 作業場を読み書き | ○ | ○ | 研究ホームを読み書き | なし |
| 大学 | 作業場を読むだけ | × | × | 授業ホームを読み書き | Box（読むだけ） |
| 仕事 | 作業場を読むだけ | × | × | なし | Microsoft 365（読むだけ） |
| 知識 | 作業場を読むだけ | × | 質問に答えるときだけ | なし | なし |

- 表は各モジュールの `module.toml` の `[actor]` から作り、実行のときにコアが守らせる
- 守らせ方は [architecture.md](architecture.md#ai-の動かし方)

## どの担当にも共通

| 項目 | 中身 |
|---|---|
| 頼み方 | 決まった仕事（スキル）か、自由な質問 `ask`。スキルの一覧は名刺（`/.well-known/agent-card.json`）に載る |
| AI | 担当ごとに Claude か Codex を選ぶ（`agents.csv` の `engine`、表が無ければ App Home）。選ぶまで動かない |
| 深さ（用途） | 軽いモデルが依頼から選ぶ。依頼の頭に `[[用途]]` を書けば指定できる（例 `[[research-design]]`） |
| 会話 | 1スレッド = 1会話。AI と指示書の版が同じ間は続け、変われば Slack の履歴から始め直す |
| 上限 | 利用上限に当たったら、明けてから本体がやり直す。明ける前に書かれたものは ⏳ を付けて待たせ、明けたらその続きとしてやる |
| 様子の確認 | 「進捗は？」など様子を聞かれただけの回は、読むだけで動かす（作業は始まらない） |
| ログイン切れ | スレッドにそう書き、`#00_kei-agent` に入り直し方を1回だけ知らせる |
| 持たないもの | Slack への投稿、スレッドと会話の対応、ほかの担当の鍵、ほかの担当への連絡 |

`ask` の依頼と、全部の担当に共通の返事の形:

```json
{"prompt": "…", "session_id": "…", "channel": "C…", "thread_ts": "…", "use_case": "…", "provider": "claude"}
{"ok": true, "text": "人が読む文", "data": {}, "limit_reset_at": null, "cost_usd": 0.02}
```

## アカウントとログ

- 連携を使う担当は、その担当用の AI のアカウントで動かせる（大学は個人、仕事は会社）。設定は [deploy/README.md](../deploy/README.md#担当ごとの秘密情報)
- ログ: `~/Library/Logs/kei-agent/<名前>-launchd.log`
- 動いているかと版: `curl -s http://127.0.0.1:<番地>/.well-known/agent-card.json`、`uv run kei-agent doctor`
- 担当を足すときは [modules.md](modules.md)（`kei-agent module new <名前> --ai --process`）
