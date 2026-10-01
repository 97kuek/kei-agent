# 仕事の開発（`workdev`）

- 仕事のプロジェクトのチャンネル（`#work-<名前>`）ごとの作業場で、コードを書き、コマンドとテストを動かす
- 会社のアカウントで動く（秘密情報は仕事のもの `kei-agent-work.zsh` を読む。`module.toml` の `[process] secrets = "work"`）
- Outlook・Teams・SharePoint は読まない（メールなどの外の文と、コマンド・Web を1回の実行に揃えないため）。それは [仕事](work-agent.md) の `#30_work`

| 項目 | 中身 |
|---|---|
| チャンネル | `#work-<名前>`（番号は付けてよい。`#31_work-billing` も同じ） |
| 作業場 | `~/work/<名前>`（置き場所は `agents.csv` の workdev の行の `folder`） |
| 番地 | 8793 |
| フォルダ | `modules/workdev/`（指示書 `workdev.md`） |
| 触れる範囲 | 作業場を読み書き・コマンド・Web（[制限の表](../agents.md#触れる範囲制限の表)） |

## 始め方

1. Slack で `#work-<名前>` を作り、Kei Agent を招く
2. 「既定の場所に作る」（`~/work/<名前>`）か「既存のフォルダを使う」（手元のリポジトリ）を選ぶ。既存のフォルダは `themes.toml` に書かれる
3. 前提（言語・テストの回し方・触ってはいけないところ）を作業場の `AGENTS.md` に書く

- Git のリポジトリなら、Kei Agent の記録（`.kei-agent/`）と受け渡しのフォルダ（`inputs/`・`outputs/`）は `.git/info/exclude` に入れる
- 既存のフォルダの `AGENTS.md`・`CLAUDE.md` は動かさない
- コミットは頼まれたときだけ。push・デプロイ・本番のデータに触れる操作はしない
- 1回の作業は30分まで。ジョブ（長い処理）の仕組みは無い。接続先の許可は研究テーマと同じ（ボタンか App Home）

## AI の用途

| 用途 | 使うとき | Claude | Codex |
|---|---|---|---|
| `workdev_execute` | 実装・修正・テスト・調査 | sonnet-5 / high | sol / high |
| `workdev_design` | 設計・方針の比較・大きな作り替えの計画 | opus-5 / high | sol / xhigh |
