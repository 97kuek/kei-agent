# 仕事（`work`）

- 会社のアカウントの Microsoft 365 を読んで答える
- 送信・投稿・予定の作成・変更・削除はしない（読むだけ）
- 会議は朝の一覧・声・予定カレンダー・振り返りの材料にも載る

| 項目 | 中身 |
|---|---|
| チャンネル | `#30_work` |
| 番地 | 8789 |
| フォルダ | `modules/work/`（指示書 `work.md`、skill は `plugin/`） |
| 触れる範囲 | 作業場を読むだけ・Microsoft 365 を読むだけ（[制限の表](../agents.md#触れる範囲制限の表)） |

## 頼み方

| 言い方 | 起きること |
|---|---|
| 「今日の予定は？」「今週の予定は？」 | Outlook の会議を並べる |
| そのほか | メール・Teams・SharePoint を読んで答える。長いものは要点だけ引く |

## 読めるもの

| AI | 読める連携 |
|---|---|
| Claude | Outlook（予定・メール・人・空き時間）、Teams、SharePoint |
| Codex | Outlook のメールと予定だけ |

- 読む道具の一覧は `module.toml` の `[[actor.connectors]]`。書いた道具だけを使える

## AI の用途

| 用途 | 使うとき | Claude | Codex |
|---|---|---|---|
| `work_single_source`（既定） | 1件のメール・資料の要点 | sonnet-5 / medium | luna / medium |
| `work_cross_source` | 複数のメール・予定・資料の状況のまとめ | sonnet-5 / high | sol / medium |
| `work_decide` | 優先順位・会議の準備・論点の整理 | opus-5 / high | sol / high |

## スキル

| スキル | 中身 |
|---|---|
| `list-events` | Outlook の予定を始まる順に（既定7日。`days` で変える）。読むだけの1回として動かす |
| `ask` | 定型に当たらない質問 |

- skill（`plugin/skills/`）: `researching-work-context`（経緯を調べる）、`preparing-meetings`、`drafting-work-actions`（文面の下書きだけ）

## アカウント

| AI | つなぎ方 |
|---|---|
| Claude | `kei-agent-work.zsh` に `unset CLAUDE_CODE_OAUTH_TOKEN` と `CLAUDE_CONFIG_DIR="$HOME/.claude-work"`。そのプロファイルで claude.ai の Microsoft 365 をつなぐ |
| Codex | ChatGPT のログインで、Codex アプリの Outlook Email と Outlook Calendar をつなぐ |
