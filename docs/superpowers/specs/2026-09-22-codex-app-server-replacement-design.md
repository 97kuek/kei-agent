# Codex App Server Complete Replacement Design

## Goal

Claude の利用上限や契約状態に依存せず、KeiAgent の研究・大学・仕事エージェントを Codex で実行できるようにする。ユーザーが Codex デスクトップで接続済みのアプリ認証を再利用し、Slack App Home で agent ごとに provider と model を明示して選べるようにする。暗黙の Claude へのフォールバックは行わない。

## Observed capability

2026-09-22 にローカル Codex App Server の `app/installed` を最新化して確認した結果、Box、Notion、Microsoft Outlook Email、Microsoft Outlook Calendar はいずれも `enabled` と `callable` である。これはデスクトップの接続済みアカウントを App Server が参照できることを示す。接続名、OAuth token、ファイル内容、Notion の内容は記録しない。

`codex exec` の通常経路はこれらの App connector をモデルに公開しなかったため、外部アプリを使う agent の実行器には採用しない。

## Architecture

`src/kei_agent/codex_app_server.py` を App Server JSON-RPC の唯一の adapter とする。agent の各依頼で標準入出力の App Server 子プロセスを起動し、`initialize`、`thread/start`、`turn/start` の順に呼ぶ。入力は prompt と、許可した connector を指す `app://` mention だけである。thread と turn のイベントを既存 `RunResult` と A2A progress に変換する。

設定は provider を選ぶ既存の `AgentProfile` を拡張するが、connector の選択は単なる MCP 名ではなく、agent policy に属する App の論理名として扱う。App の内部 ID はアカウントごとに発行されるため、リポジトリには保存しない。起動時に adapter は App Server へ `app/installed` を問い合わせ、論理名を最新の内部 ID に読み取りで解決する。宣言した App が `enabled` かつ `callable` でなければ prompt を実行せず明示的に失敗する。

アプリの有効化は一回の App Server 実行に渡す一時的な Codex 設定で閉じる。最新の内部 ID を解決した後、全アプリを既定で無効にし、その agent の許可 App だけを有効化する別の App Server 子プロセスを起動する。常駐プロセスのグローバル設定や、ユーザーがデスクトップで使う有効設定を書き換えない。

## Agent policy

| Agent | Codex execution | Allowed apps | Write policy |
|---|---|---|---|
| `course` | App Server | University Box, university Notion | Box は read-only。Notion は大学ホーム配下のみ create / edit / move / duplicate / delete / database create を許可する。 |
| `work` | App Server | Outlook Email, Outlook Calendar | 読み取り専用。送信、編集、削除、SharePoint、Teams は許可しない。 |
| `research` | workspace runner + scoped Notion gateway | research-notion, W&B | 研究テーマの作業場と研究ホーム配下だけ。既存の Notion gateway を維持し、広い個人 Notion App connector を代用しない。W&B は read-first。 |

Box は大学アカウントでログイン済みの接続を使用する。Box の Custom App 作成、OAuth token の複製、別アカウントのログインは行わない。SharePoint は未接続のままなので、設定・UI・実行許可のいずれにも加えない。

Notion の「ホーム配下だけ」という制限は、単なるモデル指示に依存しない。既存の course Notion client の親子チェックを App adapter の tool-policy 層に移し、対象・移動先・複製先・新規 database の親が大学ホームの子孫であることを各書込み前に検査する。App connector が tool-level policy を公開できない場合、その書込み操作を Codex App 経路では有効化せず、対象を限定できる既存の gateway 操作に委譲する。範囲保証ができない状態で全操作を解放しない。

## Slack App Home

App Home は各 agent ごとに以下を表示・保存する。

- provider: `claude` または `codex`
- model: provider に対応するモデル名（空欄は provider の既定）
- reasoning effort
- connector readiness: 必要な App が `callable` かを名前だけで表示
- permission summary: その agent に許可された app と read/write 境界

保存は既存の設定の正規形式を通し、未知の provider、未知 agent、許可外 connector、未接続 connector を拒否する。設定変更は次の依頼から有効にし、進行中の turn の実行器を切り替えない。App Home の「Codex に切替」は readiness が成功している場合だけ操作可能にする。

## Failure handling and observability

App Server の開始、初期化、app state、thread start、turn のいずれかで失敗したときは、その依頼を `ok: false` で終える。メッセージには「どの接続が未接続か」または「Codex 実行が失敗したか」だけを出す。Claude へ自動的に切り替えない。

実行ログと post-run hook に残すのは agent、provider、model、所要時間、成功可否、opaque な thread id のみである。prompt、応答本文、tool arguments、Box / Notion / Outlook のデータ、アカウント情報、OAuth token は残さない。

## Migration

1. App Server adapter と unit tests を追加する。
2. course と work の `ask` を provider-aware な connector entrypoint に置換する。定型の Moodle / ICS / Notion API 操作は現行の専用 client を維持する。
3. research は existing workspace runner の Codex path を scoped gateway と W&B policy に合わせて完成させる。
4. Slack Home の provider/model controls と readiness 表示を追加する。
5. provider を agent ごとに Codex に設定し、read-only probe、course Box read、work Outlook read、研究 gateway scope denial を実環境で確認する。
6. 全テスト、launchd の restart、Slack mention の疎通を確認してから main へ統合・deploy する。

## Test strategy

- JSON-RPC handshake、App state parsing、mention construction、thread event から `RunResult` への変換を unit test する。
- course と work の provider dispatch を fake App Server で test し、Claude と Codex のどちらも明示された profile だけを使用することを確認する。
- policy test で、course から Outlook、work から Box / Notion / SharePoint、research から広い Notion App connector を要求すると起動前に拒否されることを確認する。
- course の書込み target / parent が大学ホーム外の場合に拒否されることを integration test する。
- 実環境では状態のみを読む `app/installed` probe の後、最小の read-only operation を人の確認のもとで実行する。学業成績、Box ファイル本文、メール本文をテストログに取り込まない。

## Non-goals

- App connector の自動インストール、OAuth login、token export/import
- SharePoint / Teams の有効化
- Claude と Codex 間の自動 fallback
- 学業成績の再インポートや Notion データの変更
