# 実行境界の統一とレガシー削除設計

## 目的

ユースケース別モデル方針の実装を、実際の実行境界でも強制する。Claude と Codex は agent ごとに対等に選べるが、model、effort、plugin、MCP、sandbox、read-only 制約は選択済み actor と use case 以外から決まらないようにする。同時に旧 recipe、固定 Codex 音声委譲、研究専用 runner 互換 API を削除する。

## 現状の問題

- `runner.run_model` が旧 `run_claude` へ research profile を注入するため、router/self_fix/voice も research plugin と Notion MCP を得る。
- allowlist と actor/use case 対応が policy にはあるが、実行直前の境界では強制されない。
- voice の read-only は Codex sandbox 名だけで、research Notion MCP と workspace 初期化を除外していない。Claude も tool permission を read-only に絞らない。
- `run_claude`、`AgentProfile` の model/effort、旧 App Home profile API、`ask_research`、`think.py`、旧 recipe 由来のテストが二重経路を残す。
- `docs/design.md` が Claude 専用・Codex 固定音声委譲のままである。
- Retro & Planning は Slack の最終出力を prompt の指示だけで制御しており、モデルが付けた作業経過の説明まで投稿しうる。さらに、利用者向けの footer にローカル絶対パスと Codex App を案内している。
- 授業ホームの文書・skill は `📊 成績履歴`、`🎓 単位要件`、`📈 GPA推移` を正本としているが、setup と状態ファイルが管理するのは `授業`、`課題`、`学習ログ` だけである。成績系 DB の schema、取り込み、関連、移行規約が実装されていない。

## 設計

### 実行仕様

`ResolvedModel` は `actor`、`use_case`、`provider`、`model`、`reasoning_effort` を保持する唯一の model 解決値とする。runner の公開実行 API はこの値を必須で受け、profile や workspace に model/effort を持たせない。

runner は actor policy から次を組み立てる。

- plugin directory: `research`、`course`、`work` のみ。それ以外の actor に plugin は渡さない。
- MCP: research の通常書込み実行だけが research Notion Gateway を得る。router/self_fix/voice read-only は持たない。
- Codex skills と connector preflight: actor が明示した connector だけを確認する。research 固有の skill/MCP を他 actor に注入しない。
- sandbox: `read_only` 実行では Codex の read-only sandbox、Claude の Read/Glob/Grep と必要最小限の read-only tool allowlist だけを渡す。workspace 作成、Edit、write MCP は実行しない。

`Workspace` は channel と filesystem scope だけを表し、model、effort、legacy recipe 名を保持しない。read-only は `ExecutionRequest` 側の実行制約に移す。

### policy と分類

`model_policy` は actor ごとの通常 use case 集合を持つ。`resolve` は actor と use case の対応を検証し、分類専用の lightweight use case だけを research/course/work actor に共通で許可する。Astra/Fable は research の `[[manual-astra]]` / `[[manual-fable]]` 経路だけにする。

`model_classifier` は lightweight recipe で JSON と confidence を受ける。未知・低信頼・技術的失敗は actor の通常 fallback recipe を返す。quota は fallback せず、provider/model/use case/reset を含む停止結果として呼び出し元へ返す。

### 音声

Realtime は `gpt-realtime-2.1-mini` 固定である。予定・状態は同期済みの手元 tool だけで答え、内容確認は `ask_agent` だけで担当 agent へ委譲する。旧 `ask_research` と `think.py` は削除する。研究の音声照会は theme を必須とし、A2A payload に read-only 実行制約を渡す。

### 定期投稿の出力境界

定期処理が Slack に投稿する文は、モデルの自由文をそのまま通さない。Retro & Planning は次だけを許可する構造化した最終出力とする。

1. `*今日の成果*`
2. `*未完了タスク*`
3. `夜間に実行したいタスクはありますか？`

各見出しの内容と最終行を parser で検証してから投稿する。前置き、進捗説明、tool や skill の名前、余分な見出しを含む出力は投稿しない。形式不正時は内容を推測・切り貼りせず、定型の失敗通知にして再実行できるようにする。

Retro の追記案内は Slack-native に統一する。Notion page が作れた場合は「このスレッドか Notion の Retro & Planning に結論を書くと明日の Daily に反映される」と短く案内し、作れなければこのスレッドだけを案内する。Codex App、ローカルファイルパス、利用者に開かせる内部作業ファイルは投稿しない。

### 授業ホームの Notion DB

授業ホームの正本は次の 6 DB とする。いずれも同じ `notion-course.json` で database ID、data-source ID、property ID を管理する。

| DB | 役割 | 関連 |
|---|---|---|
| `授業` | 現在・過去を含む科目台帳 | `課題`、`学習ログ`、`📊 成績履歴` の中心 |
| `課題` | Moodle と手入力の課題 | `科目` → `授業` |
| `学習ログ` | Kei Agent の学習時間 | `科目` → `授業` |
| `📊 成績履歴` | 科目ごとの年度・学期・単位・成績・GP・科目区分 | `科目` → `授業`、`単位要件`・`GPA推移` から参照 |
| `🎓 単位要件` | 所定・既得・算入・残り単位 | `算入成績` → `📊 成績履歴` |
| `📈 GPA推移` | 学期別・通算 GPA | `対象成績` → `📊 成績履歴` |

setup は既存の子 DB を canonical title で検出し、不足 DB と不足プロパティ・relation だけを idempotent に追加する。過去の成績を登録する `授業` 行は `終了` として残し、現在の予定・時間カードは `履修中` かつ当該学期だけに絞る。

学業記録の import はローカル HTML から派生データだけを取り込み、安定した source key で upsert する。relation は入力により一意に確定するものだけを作る。例えば成績の科目・年度・学期から既存の `授業` 行を特定できる場合は結び、単位要件の集計行から個々の成績を推測して結ぶことはしない。曖昧な既存 DB、重複、未対応 relation は migration report に出し、実データの移動・統合・アーカイブは依頼者の確認後だけにする。

### 設定・設定画面

永続設定は actor provider と connector だけにする。model/effort/profile override 用の SQLite key、setter、getter、App Home action を削除する。未選択 provider は実行を停止し、設定画面で選択を促す。

### ドキュメントとテスト

`docs/design.md`、`docs/agents*.md`、`docs/model-policy.md`、`docs/voice.md`、`docs/notion-layout.md` を新しい actor 境界、provider 方針、授業ホームの DB 構成に同期する。テストは public API のみを使い、旧 API を呼ばない。

必須の確認は以下とする。

1. actor/use case 不整合、allowlist 外、manual exception の不正 actor を拒否する。
2. actor ごとの command が plugin/MCP/effort/model を正しく受け、research 専用の権限を漏らさない。
3. Codex と Claude の read-only command/settings に Edit、workspace 初期化、write MCP が無い。
4. voice tool に `ask_agent` だけが載り、A2A payload が read-only を保持する。
5. `rg` で削除対象の旧 API が product code に存在しない。
6. Retro の余分な作業実況は Slack に投稿されず、Notion の有無に応じてローカルパスなしの案内だけを投稿する。
7. setup が 6 DB とその relation を idempotent に作成・検証し、学業記録 import が source key で upsert する。曖昧な既存データを自動で移動・削除・結合しない。
8. `uv run --group dev --group agents pytest -q` と `uvx ruff check .` を通す。

## 非目標

- provider 間の自動フォールバック、Astra/Fable の通常昇格、Realtime の別 model 設定、外部 connector の新規導入は行わない。
- 既存の A2A プロトコル互換（旧版 Agent Card の読み取り）は今回の model policy migration と無関係なので削除しない。
- 実在する Notion の DB・ページを一括で移動、削除、アーカイブすることは、この実装に含めない。まず dry-run の migration report を出し、対象ごとの確認後に別途実行する。
