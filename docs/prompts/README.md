# Dot に貼り付けるプロンプト

リンク先のコードブロック内を全文コピーし、Dot との会話へ貼って設定を依頼します。定期実行は既存の予定を指定して更新し、プロフィールの Scheduled で指示・時刻・投稿先を確認します。カスタム指示には通話の指示も含みます。各定期実行には共通ルールを含めているため、その文書だけで貼り付けられます。接続とローカルの停止対象は [Dot の設定](../dots.md#dot-側の設定) を確認します。

| 貼り付け先 | 時刻（日本時間） | 全文 | 出力先 |
|---|---|---|---|
| Dot との会話（継続指示） | — | [Slack・通話・MCP・安全と Notion の扱い](dot-custom-instructions.md) | 依頼元のスレッド・通話 |
| Dot との会話（継続指示） | — | [個人開発とプロジェクト対応表](dot-development.md) | 依頼元スレッド・PR |
| 定期実行の指示 | 07:00 | [先行研究の新着](dot-literature.md) | Slack `#0-overview`、研究ホームの「先行研究」 |
| 定期実行の指示 | 07:00 | [読みもの](dot-reading.md) | Slack `#4-knowledge`（記事ごとの親投稿） |
| 定期実行の指示 | 07:40 | [カレンダーの同期](dot-calendar-sync.md) | 共通ホームの「予定カレンダー」（異常の通知は `#0-overview`） |
| 定期実行の指示 | 08:00 | [朝の一覧と Daily](dot-daily.md) | Slack `#0-overview`、共通ホームの「日別記録」 |
| 定期実行の指示 | 08:00・18:00 | [締切の知らせ](dot-deadlines.md) | Slack `#0-overview` |
| 定期実行の指示 | 21:00 | [振り返り（Retro & Planning）](dot-review.md) | Slack `#0-overview`、共通ホームの「日別記録」「学びのノート」 |
| 定期実行の指示 | 毎時 | [Kei Agent からの知らせ](dot-notices.md) | 知らせの channel と依頼元のスレッド、研究ホームの「Task」 |
| 定期実行の指示 | 00:00 | [夜間の Task](dot-night-tasks.md) | 研究ホームの「Task」（MCP の run を使用） |

- 同名の既存予定を更新します。文書を作るだけでは、Dot やローカルの実設定は変わりません。
- 朝の読みものは `#4-knowledge` に1記事1親投稿です。3記事なら3投稿になり、それぞれのスレッドで続けられます。
- 認証情報は接続側に設定し、プロンプトには入れません。

- この一覧は Dot に設定する定期実行を網羅します。Mac に残る `intake`・`toggl_import`・`maintenance` と、API 設定時の Moodle の10分ごとの提出・受験終了の同期は機械処理で、Dot 用のプロンプトは不要です（[Kei Agent に残すもの](../dots.md#kei-agent-に残すもの)）。
