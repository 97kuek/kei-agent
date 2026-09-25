# Claude Code / Codex 共通実行契約の設計

## 目的と決定事項

Kei Agent の利用者から見える仕様を、Claude Code と Codex のどちらを選んでも一致させる。既存の Claude Code 経路を振る舞いの基準とするが、**既定 provider は設けない**。利用者が選んだ provider、用途別 model / effort recipe、利用上限は維持し、別 provider への自動切替もしない。

同じ Slack thread 内で provider を切り替える場合、切替先の内部 session を切替前の会話の続きとして再利用しない。Slack 上の会話履歴と agent に必要な状態から新しい session を始める。これにより「同じ会話を続けている」使用感を保ちつつ、Claude / Codex 固有の session による指示・権限・文脈の混入を防ぐ。

## 現状の不一致

| 領域 | Claude Code 側 | Codex 側・共通経路の課題 |
| --- | --- | --- |
| 指示 | `runner.py` が `--append-system-prompt`、agent plugin を渡す | Codex CLI は同等の共通・agent 指示を明示的に渡していない。course / work の一部は user prompt への前置きに依存する |
| skill | research / course / work の plugin を選択 | Codex CLI の skill 公開は research に限られ、app-server 経路も同じ agent skill 境界を保証していない |
| 権限 | `guard.build_settings`、限定 MCP、plugin 境界 | `--sandbox` と app / MCP の設定が別経路で、読み取り拒否・書き込み先・外部接続の同等性を一箇所で検証できない |
| session | Claude の session ID を保存・再開 | `threads.session_id` と `agent_sessions` に provider がなく、切替時や旧データの解釈が曖昧 |
| 出力 | `response_output.py` で final を検証 | raw の途中 text / activity を拾う経路が残り、provider 間で final 判定が揃っていない |
| 失敗・上限 | Claude 由来の文言と reset 判定 | `limited_until` が全 provider 共通で、片方の上限が他方の実行にも影響しうる |

これは Claude CLI と Codex CLI のオプションを字面まで揃える要求ではない。利用者向けの意味、許可された能力、失敗時の安全性を揃える。

## 実行契約

モデルを起動する前に、共通の `ExecutionContract` を組み立てる。入力は `agent`、`use_case`、利用者が選んだ `provider`、解決済み `ResolvedModel`、作業場、読み取り専用フラグ、必要な能力、会話履歴、指示の版とする。model / effort を adapter が上書きしない。router、research、course、work と定期処理も同じ契約を通る。

契約は次を明示する。

1. **指示と skill**: 共通の Kei Agent 指示、agent 固有の指示、必要な skill の集合とその版。agent の recipe を skill が継承し、明示された例外だけ専用 recipe を使う。
2. **能力と権限**: 読み取り・書き込み範囲、禁止パス、ネットワーク接続先、許可された MCP / app / tool。必要能力と実際に強制可能な能力を区別する。
3. **会話**: provider と指示版が一致する session のみ再開可能。切替時は Slack 履歴を公開可能な会話文脈へ正規化して渡す。
4. **結果**: final 本文、session ID、使用量・時間、tool activity、分類済み失敗を共通 `RunResult` に正規化する。生イベントは表示層へ渡さない。

`ClaudeCodeAdapter` は現在の Claude Code の指示、plugin、sandbox、MCP をこの契約へ対応づける。`CodexAdapter` は CLI と app-server の差を内部で吸収し、共通契約に対応できない能力がある場合は起動前に失敗する。router のような限定実行も例外扱いではなく、最小権限の契約として表す。既存の Claude 側動作を変更する場合は、互換性テストで意図を明示する。

### 指示と skill の同等性

`prompts/system.md` と agent ごとの prompt を正本とする。Claude Code は既存の system prompt / plugin を使う。Codex CLI は同じ内容を適切な優先度の開発者指示として渡し、対応する agent skill だけを作業場で発見可能にする。course / work の app-server 経路は、現在 user prompt に前置きしている guide を共通の agent 指示として扱い、個別質問や JSON 出力要求と衝突させない。研究用の skill / gateway を他 agent に公開しない。

指示版は共通・agent 別に計算する。再開した session に新しい指示が適用できない場合は、版を明示して更新できる方式を検証する。更新を保証できなければ新規 session を開始する。旧指示のまま黙って実行しない。

### 能力と権限

Claude 側の現在の許可範囲を基準として能力表を定義する。Codex の CLI sandbox だけで同等とみなさず、ファイルの read deny / write root、ネットワークの domain allowlist、MCP、hosted app を別々に検査する。Codex で domain 制限に network proxy が必要ならその有効化を preflight で確認する。CLI で強制できない hosted tool や app の能力は、それぞれ独立した許可表と接続状態を確認する。必要な強制が実現できない実行は fail closed とし、権限を広げて続行しない。

研究 Notion gateway や course / work の接続は用途ごとに限定し、秘密情報を prompt、Slack、通常ログに出さない。読み取り専用処理では provider に関係なく書き込み能力を渡さない。能力不足を別 provider への自動 fallback で隠さず、利用者向けには短い接続・設定エラーを出す。

### session と会話の引き継ぎ

保存鍵を少なくとも `(channel, thread_ts, agent, provider)` とし、session ID と指示版を一緒に保存する。主会話と A2A agent session の両方に適用する。同一 provider に戻った場合でも、間に別 provider の発話があれば古い session をそのまま再開しない。Slack 履歴を再構成し、その時点の provider で新規 session を開始する。履歴は発言者・時刻・公開済み本文だけを対象とし、内部思考、tool 出力、秘密、失敗ログを含めない。長い履歴は欠落を隠す要約を作らず、明示した上限・切り詰め方をテストで固定する。

既存 DB の provider 不明な session ID は推測して再開しない。移行時は ID を破棄せず旧値として保持し、最初の対象実行で安全な新規 session に切り替える。Slack に同じ通知を二重投稿しない。通常の同一 provider・同一指示版の継続は従来どおり再開できる。

### 出力、失敗、利用上限

Slack へ出すのは `response_output.py` が検証した final と固定の進捗文だけにする。Claude / Codex の stream 中の `agent_message`、activity、コマンド、内部エラーを表示・保存用本文へ混ぜない。Daily / Retro / 通常会話 / A2A の既存契約と Kei Agent の口調を同じ境界で検証する。非ゼロ終了、timeout、session 消失、出力欠落、quota は provider に関係なく失敗として分類し、途中 text があっても成功扱いにしない。

quota は provider ごとに保持し、選択した provider の処理だけを延期する。reset 時刻が確定できない場合の再試行方針は provider ごとに持つ。Claude 固有の文言を汎用 UI に出さない。定期処理はその時点で選択された provider の recipe を使い、もう一方の上限状態から影響を受けない。課金・使用量が provider から取れない場合は不明として記録し、ゼロと偽らない。

## 検証と受け入れ条件

1. router / research / course / work と主要な定期処理について、両 provider の指示版、skill、能力、model / effort を契約テストで比較できる。
2. Claude から Codex、Codex から Claude、同 provider 継続、旧 DB からの初回実行をテストし、誤った session を再開せず Slack 文脈が保たれる。
3. 禁止 read、範囲外 write、未許可 domain / app / MCP、read-only での write を各 adapter で拒否する。実環境で強制できない場合は起動前に失敗する。
4. success / nonzero exit / timeout / quota / session 消失 / 不正 final の fixture を両 provider で通し、Slack には検証済み final または安全な固定失敗文しか出ない。
5. 片方の quota がもう片方の会話・定期処理を止めない。自動 fallback と暗黙の provider 既定値を作らない。
6. mock による CI テストに加え、実 provider の read-only canary で指示、skill 発見、権限、session 切替を確認する。外部書き込みを伴う検証は別途対象を限定する。

## 実装順序と対象外

実装時は、共通契約とテスト fixture、指示・skill 配布、能力 preflight、session 移行、出力・quota 正規化、実 provider canary の順に進める。各段階で Claude 側の既存挙動を回帰テストする。全経路の確認前に旧コードや旧 DB 列を削除しない。デプロイや DB 変更は実装計画で手順と復旧方法を確定する。

この設計では model の再選定、provider の自動選択、利用料金の最適化、Claude / Codex 内部の完全な機能同一化、過去の Slack 投稿の修正を行わない。Kei Agent が保証するのは、選択された provider で契約上必要な能力を安全に実行し、同じ利用者向け仕様を提供することまでとする。

## 参照

- 既存実装: `src/kei_agent/runner.py`、`src/kei_agent/codex_app_server.py`、`src/kei_agent_a2a/claude.py`、`src/kei_agent/assistant.py`、`src/kei_agent/store.py`、`src/kei_agent/response_output.py`
- 既存の出力境界: `docs/superpowers/specs/2026-09-24-slack-output-boundary-design.md`
- Codex 設定: <https://learn.chatgpt.com/docs/config-file/config-reference>
- Codex 権限: <https://learn.chatgpt.com/docs/permissions>、<https://learn.chatgpt.com/docs/agent-approvals-security>
