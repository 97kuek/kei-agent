# エージェント別 skills・hooks 設計

## 目的

研究・大学・仕事の各エージェントに専用の skill を持たせ、反復する手順を prompt から分離する。
同時に、各ドメインの権限境界を plugin と hook で分離し、別ドメインの skill や外部サービスを
誤って使わない構成にする。

## 基本方針

- エージェントごとに独立した Claude Code plugin を持つ。
- 各 Claude プロセスには担当 plugin だけを `--plugin-dir` で渡す。
- prompt には人格、権限境界、返答形式など常時必要な規則だけを残す。
- 検索・操作・保存など、依頼に応じて選ぶ反復手順は `SKILL.md` に置く。
- 既存の sandbox と `--allowedTools` / `--disallowedTools` を第一防御とし、hook は明確な安全違反を
  拒否する第二防御にする。
- 外部サービスの認証情報を skill や作業用 sandbox に渡さない。

## plugin の構成

```text
plugin/
├── research/
│   ├── .claude-plugin/plugin.json
│   ├── skills/
│   │   ├── running-jobs/
│   │   ├── researching-literature/
│   │   └── managing-research-notion/
│   └── hooks/
├── course/
│   ├── .claude-plugin/plugin.json
│   ├── skills/
│   │   ├── finding-course-materials/
│   │   ├── managing-assignments/
│   │   └── managing-course-notion/
│   └── hooks/
└── work/
    ├── .claude-plugin/plugin.json
    ├── skills/
    │   ├── researching-work-context/
    │   ├── preparing-meetings/
    │   └── drafting-work-actions/
    └── hooks/
```

既存の `job` と `literature` は、それぞれ `running-jobs` と `researching-literature` に移す。
スクリプトは対応する skill の `scripts/` に置き、`PLUGIN_ROOT` を基準に参照する。

## skill の責務

### 研究

| skill | 責務 |
|---|---|
| `running-jobs` | 数分以上かかる処理の投入、状態確認、取消、終了後の成果物確認 |
| `researching-literature` | arXiv・Semantic Scholar の検索、論文メモの保存、既存メモとの重複回避 |
| `managing-research-notion` | 研究ホーム配下のページ、ブロック、データベースの検索と全操作 |

### 大学

| skill | 責務 |
|---|---|
| `finding-course-materials` | Box の要項、授業資料、過去問を検索し、原典 URL と根拠を返す |
| `managing-assignments` | Notion の課題を確認し、状態や内容を更新する |
| `managing-course-notion` | 授業ホーム配下のページとデータベースを作成、更新、削除、移動、複製する |

### 仕事

| skill | 責務 |
|---|---|
| `researching-work-context` | メール、Teams、SharePoint を横断して根拠つきで調査する |
| `preparing-meetings` | 予定、参加者、関連メール、資料を集めて会議準備をまとめる |
| `drafting-work-actions` | 返信、Teams 投稿、予定の案を作る。外部サービスへの書き込みは行わない |

skill の description は使用条件だけを簡潔に書き、手順の要約を入れない。各 `SKILL.md` は原則500語以内とし、
大きなツール一覧やスキーマだけを `references/` に分離する。

## 権限モデル

| エージェント | サービス | 許可 |
|---|---|---|
| 研究 | ローカルファイル・Bash | 既存のテーマ別 sandbox の範囲 |
| 研究 | Notion | 研究ホーム配下で全操作 |
| 大学 | Box | 読み取り専用 |
| 大学 | Notion | 授業ホーム配下で全操作 |
| 仕事 | Microsoft 365 | 読み取り専用 |

大学は専用 `CLAUDE_CONFIG_DIR` のアカウントに授業ホームだけを共有する。コネクタの Notion 操作ツールは
全種類を利用可能にするが、共有範囲の外には Notion 側の権限で到達できない構成にする。

仕事は現在の読み取りツール allowlist を維持する。返信、投稿、予定変更などは文章案だけを返し、実行ツールを
allowlist に追加しない。

## 研究 Notion MCP ゲートウェイ

研究 Claude には `NOTION_TOKEN` を渡さない。Kei Agent 側の独立プロセス `kei-agent-notion-gateway` が
`127.0.0.1:8791/mcp` に、研究専用の Streamable HTTP MCP サーバーを提供する。launchd では
`com.kei-agent.notion-gateway` として常駐させ、`NOTION_TOKEN` とNotionの状態ファイルを読めるのは
このプロセスだけにする。研究 plugin だけがこの MCP を利用する。

### 公開する操作

- ページ、ブロック、データベースの検索と取得
- ページ、ブロック、データベースの作成
- プロパティ、本文、スキーマの更新
- ページとブロックの削除・アーカイブ
- ページの移動と複製
- データソースのクエリ

ツールは生の任意 HTTP リクエストを受け付けない。操作ごとに型のある MCP tool を公開する。

### 境界検証

- `notion.json` の `home_page_id` を研究ホームの root とする。
- 読み取りと変更の前に、対象ページまたはデータベースが root の子孫であることを確認する。
- 新規作成の親も root の子孫に限る。
- 移動元と移動先、複製元と複製先の双方を検証する。
- 親子関係は Notion API から取得し、1リクエスト中だけキャッシュする。
- 親チェーンが循環する、取得不能、上限を超える場合は拒否する。推測で許可しない。

### 認証と監査

- MCP は loopback のみに bind する。
- 研究プロセスには生のNotionトークンではなく、ゲートウェイ専用トークンだけを渡す。
- ゲートウェイトークンは研究ホーム全操作だけを許可し、ほかの API には使えない。
- 監査ログには時刻、操作名、対象 ID、成功・失敗、エラー種別だけを記録する。
- Authorization、本文、プロパティ値、検索結果は監査ログに記録しない。
- ゲートウェイ専用トークンが空ならサーバーは起動しない。health endpoint 以外はBearer認証を必須にする。

## hooks

hook は各 plugin に同梱し、`PreToolUse` で対象操作を検査する。通常の読み取りや研究のローカル作業には介入しない。

### 研究 hook

- 生の `NOTION_TOKEN` を参照または出力するコマンドを拒否する。
- research-notion MCP 以外の Notion connector/tool 呼び出しを拒否する。
- ファイルとBashの境界は既存 sandbox に任せ、同じ判定をhookへ重複実装しない。

### 大学 hook

- Box のアップロード、作成、移動、コピー、更新、コメント操作を拒否する。
- Notion 操作は拒否しない。授業ホームの境界は専用プロファイルとNotion側の共有設定で強制する。

### 仕事 hook

- メール送信、Teams投稿、予定やファイルの作成・更新・削除を拒否する。
- 検索、取得、一覧、本文閲覧は通す。

hook が入力を解釈できない場合、対象が書き込み系ツールなら拒否し、読み取り系ツールなら既存 allowlist の判定へ委ねる。
hook 自身のログにも tool input の本文や認証情報を残さない。

## 起動経路

- `Config` は `research_plugin_dir`、`course_plugin_dir`、`work_plugin_dir` を返す。
- 研究の `runner.build_command()` は研究 plugin だけを `--plugin-dir` に渡す。
- 研究の起動時だけ、research-notion MCP のURLとBearer headerを持つJSONを `--mcp-config` で渡す。
  `--strict-mcp-config` を併用し、ユーザーやプロジェクトの別MCPを読み込まない。
- 大学と仕事の `ask_connector()` は呼び出し元から plugin directory を受け取り、`--plugin-dir` に渡す。
- connector 実行では `Skill` を利用可能にするが、外部ツールは既存のエージェント別 allowlist を維持する。
- router、voice、自己改善など、今回の3ドメイン以外にはこれらの plugin を渡さない。

## prompt の整理

`prompts/system.md`、`prompts/course.md`、`prompts/work.md` から、各 skill と重複する操作手順を削る。
次は prompt に残す。

- エージェントの役割と一人称
- 返答の長さと Slack 向け書式
- ドメイン外へ出ない権限境界
- 原典確認が必要な判断
- 人の承認が必要な行為
- 不明時に推測しない規則

## エラー処理

- 研究 Notion MCP が停止中なら、Notion 操作を実行せず「ゲートウェイに接続できない」と返す。
- 研究ホーム外の対象は永久失敗として拒否し、別経路へフォールバックしない。
- 大学・仕事のコネクタが見えない場合は、専用プロファイルへのログインと連携状態の確認を案内する。
- hook が書き込み操作を拒否した場合は、拒否した操作種別と許可された代替手段だけを返す。
- Notion の 429 と 5xx は既存 `Notion` クライアントの上限つき再試行を使う。

## テスト

### plugin と skill

- 各 plugin manifest と全 `SKILL.md` の frontmatter を検証する。
- skill description が担当依頼に一致し、別ドメインの依頼に一致しないことをケース表で検証する。
- 研究、大学、仕事の起動コマンドに担当 plugin だけが含まれることを検証する。
- connector の許可ツールに `Skill` と担当外ではない外部ツールだけが含まれることを検証する。

### Notion gateway

- 研究ホーム自身と全階層の子孫に対する各操作を許可する。
- ホーム外、親不明、循環、探索上限超過を拒否する。
- 作成先、移動元・先、複製元・先をすべて検証する。
- gateway停止時にClaudeが生トークンや別コネクタへフォールバックしない。
- 監査ログにトークン、本文、プロパティ値が含まれない。

### hooks

- 大学のBox書き込みと仕事の外部書き込みを拒否する。
- 大学Notionの全操作、仕事の読み取り、研究Notion MCPを許可する。
- 不完全なtool inputに対するfail-closed / allowlist委譲を検証する。

### 全体

- 既存のA2A往復、研究ジョブ、大学・仕事connector、音声テストを維持する。
- `pytest`、Ruff、`git diff --check` を通す。
- 本物の外部サービスはテストで呼ばず、Notionとconnectorを偽物に差し替える。

## 移行

1. 既存 research plugin を `plugin/research/` へ移し、研究起動経路だけを切り替える。
2. 大学・仕事 plugin と skill を追加し、connector 実行へ個別 plugin を渡す。
3. 大学Notionの allowlist を全操作へ広げ、Boxと仕事の読み取り制限を維持する。
4. 研究Notion MCPと境界検証を追加する。
5. gateway用launchd設定と秘密情報の設定手順を追加する。
6. 各 plugin のhookを有効化する。
7. promptと運用文書を実装へ合わせる。

切替途中でも、担当外pluginを同じClaudeプロセスへ複数ロードしない。

## 対象外

- 仕事エージェントによるメール送信、Teams投稿、予定・ファイル変更
- Boxへの書き込み
- voiceエージェントのskill化
- A2A skill IDや共通 envelope の変更
- Notion以外の研究用外部サービスゲートウェイ
