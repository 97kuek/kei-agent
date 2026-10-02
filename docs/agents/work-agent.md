# 仕事（`work`）

- 会社のアカウントで動く1つの担当。チャンネルで、することが2つに分かれる
  - `#3-work` … 会社のアカウントの Microsoft 365 を読んで答える
  - `#work-<名前>`（プロジェクトのチャンネル）… プロジェクトの作業場で、コードを書き、コマンドとテストを動かす
- Microsoft 365 は、どちらのチャンネルでも読むだけ。送信・投稿・予定の作成・変更・削除はしない
- 会議は朝の一覧・声・予定カレンダー・振り返りの材料にも載る

| 項目 | 中身 |
|---|---|
| チャンネル | `#3-work` と `#work-<名前>`（番号は付けてよい。`#3-work-billing` も同じ） |
| 作業場 | `#3-work` は状態の置き場の `agents/work`。プロジェクトは `~/work/<名前>`（置き場所は `agents.csv` の work の行の `folder`） |
| 番地 | 8789 |
| フォルダ | `modules/work/`（指示書 `work.md`、skill とフックは `plugin/`、`#3-work` の作業場のひな形 `AGENTS.template.md`） |
| 触れる範囲 | 作業場を読み書き・コマンド（会社のデータなので、Web もコマンドの通信も無し。外の文書が要るときは頭が調べて渡す）・Microsoft 365 を読むだけ（[制限の表](../agents.md#触れる範囲制限の表)） |
| 1回の上限 | `#3-work` は5分。プロジェクトは `config.toml` の `run_timeout_minutes`（既定30分） |

## 頼み方

| 言い方 | 起きること |
|---|---|
| `#3-work` で「今日の予定は？」「今週の予定は？」 | Outlook の会議を並べる |
| `#3-work` でそのほか | メール・Teams・SharePoint を読んで答える。長いものは要点だけ引く |
| `#work-<名前>` で頼む | その作業場でコードを書き、テストを動かして結果を返す。仕様や経緯はメール・Teams・資料も読む |

## 読めるもの

| AI | 読める連携 |
|---|---|
| Claude | Outlook（予定・メール・人・空き時間）、Teams、SharePoint |
| Codex | Outlook のメールと予定だけ |

- 読む道具の一覧は `module.toml` の `[[actor.connectors]]`。書いた道具だけを使える
- 二の柵（`plugin/hooks/policy.py`）が、Microsoft 365 の書く道具とほかの連携・Notion を断る

## プロジェクトのチャンネル

1. Slack で `#work-<名前>` を作り、Kei Agent を招く
2. 「既定の場所に作る」（`~/work/<名前>`）か「既存のフォルダを使う」（手元のリポジトリ）を選ぶ。既存のフォルダは `themes.toml` に書かれる
3. 前提（言語・テストの回し方・触ってはいけないところ）を作業場の `AGENTS.md` に書く

- Git のリポジトリなら、Kei Agent の記録（`.kei-agent/`）と受け渡しのフォルダ（`inputs/`・`outputs/`）は `.git/info/exclude` に入れる
- 既存のフォルダの `AGENTS.md`・`CLAUDE.md` は動かさない
- コミットは頼まれたときだけ。push・デプロイ・本番のデータに触れる操作はしない
- パッケージや外の文書が要るときは、何が要るかを書いて `❓ 確認:` で止まる（通信が無いため）
- ジョブ（長い処理）の仕組みは無い

## AI の用途

| 用途 | 使うとき | Claude | Codex |
|---|---|---|---|
| `work_single_source` | 1件のメール・資料の要点（重さ light） | sonnet-5 / medium | luna / medium |
| `work_cross_source` | 複数のメール・予定・資料の状況のまとめ | sonnet-5 / high | sol / medium |
| `work_decide` | 優先順位・会議の準備・論点の整理 | opus-5 / high | sol / high |
| `work_execute`（既定） | コードの実装・修正・テスト・調査（重さ normal） | sonnet-5 / high | sol / high |
| `work_design` | コードの設計・方針の比較・大きな作り替えの計画（重さ deep） | opus-5 / high | sol / xhigh |

- Slack の自由な依頼は、軽いモデルが依頼の文からこの中の1つを選ぶ。選べなければ既定
- 予定を読む回（`list-events`）は、いつも `work_single_source`

## スキル

| スキル | 中身 |
|---|---|
| `list-events` | Outlook の予定を始まる順に（既定7日。`days` で変える）。読むだけの1回として動かす |
| `ask` | 定型に当たらない質問。プロジェクトのチャンネルからは、作業場のチャンネルの名前が添えて届き、その作業場で動く |

- skill（`plugin/skills/`）: `researching-work-context`（経緯を調べる）、`preparing-meetings`、`drafting-work-actions`（文面の下書きだけ）

## アカウント

| AI | つなぎ方 |
|---|---|
| Claude | `agents.csv` の work の行の `claude_account` に `~/.claude-work`。そのプロファイルで claude.ai の Microsoft 365 をつなぐ |
| Codex | ChatGPT のログインで、Codex アプリの Outlook Email と Outlook Calendar をつなぐ |

- `agents.csv` の work の行の `channels` は、`work-*` の形をプロジェクトのチャンネルに、ほかを `#3-work` の名前にする（例 `work work-*`）。書かなかったほうは既定のまま
