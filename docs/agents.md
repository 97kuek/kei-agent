# Kei Agent のエージェント体系

Kei Agent は、Slackの入口を持つオーケストレーターと、研究・大学・仕事のドメインエージェントで構成する。この文書は全体の共通契約と増設時の決まりを定める。日常運用と責務の詳細は次を正とする。

| 担当 | 文書 | 一言でいうと |
|---|---|---|
| オーケストレーター | [orchestrator.md](agents/orchestrator.md) | Slack、振り分け、状態、再試行 |
| 研究 | [research.md](agents/research.md) | 研究作業、研究ホーム、W&B、実験 |
| 大学 | [course.md](agents/course.md) | 授業・成績・単位・Moodle・Box |
| 仕事 | [work.md](agents/work.md) | 会社の予定・メール・資料を読む |
| モデル運用 | [model-policy.md](model-policy.md) | レシピ、評価、更新、ロールバック |

以下は、新しいエージェントを足す場合にも全員が守る共通契約である。

- Kei Agent は「オーケストレーター＋ドメインごとのエージェント」の形で増えていく（構成は `architecture.png`、
決めた経緯は `design.md` の11章を参照）
- エージェントが増えるたびに形がずれると、どこに何があるか分からなくなる
- **この文書は参考ではなく、守る決まり**

## 1. 層

| 層 | いまあるもの | LLM | 役目 |
|---|---|---|---|
| 判断 | Kei Agent 本体（`src/kei_agent/`） | 持つ | Slack の受け口、意図の判定、返事の組み立て、柵、Notion（研究）、決まった時刻の処理、上限の管理 |
| 実行 | 研究エージェント（`src/kei_agent_research/`） | 動かすが考えない | 渡された依頼文で `claude -p` または `codex exec` を1回動かし、経過と結果を返す。長い処理（pueue の待ち行列）もこちらが持つ |
| 道具＋自分の判断 | 大学エージェント（`src/kei_agent_course/`） | 自分の claude を持つ | ドメインの外部サービス（Moodle・Box・Toggl・Notion の授業）を触り、そのドメインの質問に答える |

- **ドメイン1つ＝エージェント1つ** 研究・大学・仕事（予定）。ドメインの中をさらに割らない。

判断を持たせてよいのは、そのドメインの中だけ。**依頼者への約束（上限で待つ、やり直す）と、どのエージェントに
振るかの判定は、本体が1か所で持つ。**

## 2. 名前をそろえる

エージェントの名前（`<agent>`。`course`、`research`、`work`…）を、すべての場所で同じにする。

| もの | 形 | 例（大学） |
|---|---|---|
| パッケージ | `src/kei_agent_<agent>/` | `src/kei_agent_course/` |
| 起動のコマンド | `kei-agent-<agent>`（`pyproject.toml` の `[project.scripts]`） | `kei-agent-course` |
| 依存のグループ | `[dependency-groups] <agent> = [{ include-group = "agents" }]` | `course` |
| launchd | `com.kei-agent.<agent>`（テンプレートは `deploy/com.kei-agent.<agent>.plist.template`） | `com.kei-agent.course` |
| 起動スクリプト | `deploy/run-<agent>.sh`（`deploy/install.sh <agent>` で登録） | `deploy/run-course.sh` |
| ログ | `~/Library/Logs/kei-agent/<agent>-launchd.log` | `course-launchd.log` |
| 秘密情報 | `~/.config/zsh/local/kei-agent-<agent>.zsh`（600） | Box と Moodle と授業の Notion |
| Claude のアカウント | `CLAUDE_CODE_OAUTH_TOKEN`（枠だけ分けるとき）か `CLAUDE_CONFIG_DIR`（連携も要るとき） | 仕事は会社のアカウント専用のプロファイル |
| 住所 | `config.toml` の `[a2a.agents]` に `<agent> = "http://127.0.0.1:87xx"` | `course = "…:8787"` |
| ポート | 8787 から順番（大学 8787、研究 8788、仕事 8789 の予定） | 8787 |
| 作業場（claude を持つとき） | `~/<agent>/`（研究だけは `~/research/<テーマ>/`） | `~/course/` |
| 指示書（claude を持つとき） | `prompts/<agent>.md` | `prompts/course.md` |
| Slack のチャンネル | 番号帯（00 = 本体、10 = 研究テーマ、20 = 大学、30 = 仕事） | `#20_course` |

## 3. 口の形

### 名刺（`card.py`）

`/.well-known/agent-card.json` に出す。スキルの `id` は **kebab-case の「動詞-目的語」**（`sync-assignments`、
`list-due`、`run-claude`）。自由な質問の窓口は、どのエージェントでも **`ask`** という名前にする
（振り分け係が「定型に当てはまらなければ `ask`」と決め打ちできる）。

### 頼み方

JSON-RPC の `metadata` に `skill` と、細かい指定（`days` など）を入れる。本文は、そのスキルが決めた形
（素の文か JSON）。数分以上かかる仕事は `SendStreamingMessage` で流しながら返す（`ask` と `run-claude` は自動でそうなる）。

### 返事（封筒）

**すべてのスキルが同じ封筒で返す**（`src/kei_agent_a2a/envelope.py`）。

```json
{"ok": true, "text": "人が読む文", "data": {}, "limit_reset_at": null, "cost_usd": 0.02}
```

- `text` … 本体がそのまま Slack に出せる文
- `data` … 組み立て直すための中身（締切の一覧、`RunResult` など）。見せ方は本体が決める
- `limit_reset_at` … Claude の上限に当たったときの明ける時刻。**待つ・やり直すの約束は本体**（`Assistant.note_limit`）
- `cost_usd` … 分かるときだけ

できなかったときは A2A のタスクを `failed` にし、封筒は `ok: false` にする（`envelope.failure`）。

## 4. claude を持たせるとき

### CLIとモデルの選択

`config.toml` の `[agents.<agent>]` が実行器と既定レシピの正であり、`[model_recipes.<name>]` が実モデル名と推論強度の正である。skill は手順を定義し、モデル名を埋め込まない。更新手順は [model-policy.md](model-policy.md) を参照する。

```toml
[agents.research]
provider = "claude"       # または "codex"
default_recipe = "standard"
connectors = []            # Codexで使う有効なMCP名だけを明示する

[model_recipes.standard]
provider = "codex"
model = "gpt-5.6-terra"
reasoning_effort = "high"
```

agent単位でClaude/Codexへ切り替えられる。Codexは `codex exec --json --sandbox workspace-write` のJSONLを共通の `RunResult` に変換する。認証情報を環境変数やリポジトリに写さず、各CLIのログイン状態を使う。レシピのproviderとagentのproviderが一致しなければ起動しない。暗黙のモデル切り替えや、契約上限時の別モデルへの自動フォールバックはしない。

Codexで外部 connector を使うときは、`connectors` に名前を明示し、`codex mcp list --json` で有効と確認できるものだけを列挙する。実行前には agent ごとの許可範囲と照合し、未接続・担当外なら起動しない。許可範囲は研究が `research-notion` と `wandb`、大学が `notion` と `box`、仕事が読み取り専用の `microsoft-365`。SharePoint はこの経路に含めない。connector の自動インストール、OAuth、認証情報・URLの移行はしない。

Codexの接続可否は環境ごと・時期ごとに変わるため、この文書へ検出結果を固定しない。起動時のpreflightで、`connectors` に宣言したものがagentの許可範囲と実際の接続一覧の両方を満たすことを確認する。満たさない場合はそのproviderで起動せず、別providerへ自動で切り替えない。

この切り替えが現時点で直接適用されるのは、研究のsandbox実行器である。研究テーマの作業場はリポジトリ外なので、Codex起動前に `plugin/research/skills` だけを `<テーマ>/.agents/skills` へ相対シンボリックリンクとして公開する。既存の利用者skillは上書きしない。大学・仕事の `ask` は、Notion／Box／Microsoft 365のアカウント連携を使うため、Codex側の同等MCPコネクタを確認するまではClaude connectorを正とする。

動かし方は2つある。**どちらも `src/kei_agent_a2a/claude.py` を使い、自分で `claude` を起動する処理は書かない。**

| | 使う関数 | 何ができるか | 使うとき |
|---|---|---|---|
| **道具だけ** | `ask_connector()` | アカウントに付いている連携（Box・Notion・Microsoft 365）を使う。Bash もファイルも使えない | 外のサービスを使って答える（大学・仕事の `ask`） |
| **sandbox** | `run()` | テーマのディレクトリでファイルを読み書きし、Bash を使う | 手元で作業する（研究の `run-claude`） |

「道具だけ」のほうは、使ってよい道具を名指しで並べる（読むものだけ）。触れる先が連携に限られるので、
sandbox を締めるより結果的に狭い。連携はログイン（プロファイル）に付いてくるので、
`CLAUDE_CONFIG_DIR` でそのドメインのアカウントを指す。

### sandbox で動かすとき

- 柵は `config.toml` から組む（`kei_agent.guard`）。接続先は**そのドメインに要るものだけ**を `Workspace.allowed_domains` に渡す
- 指示書は `prompts/<agent>.md`（`Workspace.system_prompt`）。学期ごとに変わる前提は作業場の `CLAUDE.md` に置く
- 上限時間は `Workspace.timeout_minutes`（大学は5分、研究は30分）
- Box は読み取り専用。大学の Notion は授業ホームの範囲で、削除・移動・複製・データベース作成を含む操作を許可する
- コネクタの道具だけでは対象ページを限定できない。大学は専用 Claude プロファイル、Notion 側の共有範囲、
  `prompts/course.md` の3つで権限を狭める
- ほかのドメインの鍵は、子プロセスに渡さない（`guard.strip_env`。Slack・Notion・Box・Moodle・Microsoft・Toggl）
- **外部サービスを足すときは、まずコネクタ（アカウントに付いている連携）を見る。**
  すでに繋がっていれば、自前の登録は要らない（Outlook はこれで解決した）。ただし
  **定期実行で機械的に取りたいもの**は自前で書く（コネクタは claude 経由でしか呼べず、
  毎回モデルを通すことになる。Box は自前にした）
- **連携はログインに付いてくる。`claude setup-token` の長期トークンでは使えない。**
  ドメインごとに別のアカウントで動かすときは `CLAUDE_CONFIG_DIR` で専用のプロファイルを作る
- **アカウントに付いている連携（claude.ai のコネクタ）を使うときは `claude.ask_connector`**。
  連携はユーザー設定を読み込まないと見えないので、そこだけ設定を読み、使ってよい道具を名指しで並べる
  （Bash・ファイル・Web は断る）。ドメインごとに Claude のアカウントを分けられる

### 外部サービスのトークンを claude に渡さないとき（ゲートウェイ）

研究の `claude -p` は sandbox の中で Bash を使える。その中に Notion のトークンを置くと、
研究ホームの外まで届いてしまう。そこで **トークンを持つ小さなサーバーを別に立て、claude には
そこへの入口だけを渡す**。

| もの | 中身 |
|---|---|
| プロセス | `kei-agent-notion-gateway`（`src/kei_agent_notion_gateway/`） |
| 住所 | `http://127.0.0.1:8791/mcp`（loopback だけ。`/health` 以外は Bearer 認証） |
| 合言葉 | `KEI_AGENT_NOTION_GATEWAY_TOKEN`。空なら起動しない。Notion の API には使えない |
| 渡し方 | 研究の起動だけ `--mcp-config` と `--strict-mcp-config` を付ける。`NOTION_TOKEN` は渡さない |
| 範囲 | 対象と親が研究ホームの子孫か、Notion に問い合わせて確かめる。外・親不明・循環・深すぎは断る |
| tool | 操作ごとに型のある11個だけ。任意の method と path を受ける tool は置かない |
| 記録 | 時刻・操作名・対象のID・成否・失敗の種類だけ。本文、プロパティの値、検索結果、合言葉は残さない |

止まっているときは「ゲートウェイにつながらない」と返す。**別のトークンや別の Notion 連携に
乗り換えない**（乗り換えられると、範囲を狭めた意味がなくなる）。

### skill と hook（`plugin/<agent>/`）

prompt には**いつでも要る規則**（役割、権限の境界、返事の形、原典を確かめること）だけを残し、
**依頼によって使う手順**は `SKILL.md` に置く。

```text
plugin/<agent>/
├── .claude-plugin/plugin.json   # name は kei-agent-<agent>
├── skills/<skill 名>/SKILL.md   # description は「Use when…」から書く。本文は500語まで
└── hooks/{hooks.json,policy.py} # PreToolUse。第二防御
```

- **担当の plugin だけを渡す。** `--plugin-dir plugin/<agent>` と名指しする。`plugin/` そのものを
  渡すと中の plugin を全部読んでしまう
- skill の名前は **動詞-目的語**（`running-jobs`、`finding-course-materials`）。呼ぶときは
  `kei-agent-<agent>:<skill>`
- skill のスクリプトは、その skill の `scripts/` に置く。環境変数を前提にしない
- hook は「明らかな安全違反」だけを断る（exit 2）。sandbox や `--allowedTools` と同じ判定を書き足さない。
  読み取れない入力は、書き込み系なら断り、読み取り系は allowlist に任せる
- hook の出力に tool の中身や鍵を出さない。断った操作の種類と、代わりの手だけを返す
- router・voice・自己改善には、これらの plugin を渡さない

## 5. 増やすときの手順

1. `src/kei_agent_<agent>/` に `card.py`（名刺）、`executor.py`（仕事）、`app.py`（`kei_agent_a2a.server.serve` を呼ぶだけ）
2. `pyproject.toml` に `[project.scripts]`、`[dependency-groups]`、`module-name` を足す
3. `config.toml` の `[a2a.agents]` に住所を足す
4. `deploy/run-<agent>.sh` と `deploy/com.kei-agent.<agent>.plist.template` を作り、`deploy/install.sh` の `case` に名前を足す
5. 秘密情報を `~/.config/zsh/local/kei-agent-<agent>.zsh` に置く（600）
6. 本体側の取り次ぎ（`src/kei_agent/<agent>.py`）と、Slack のチャンネル（`config.toml` の `[channels]`）
7. テストは**本物のサーバーを立てて往復を見る**（`tests/test_a2a.py`、`tests/test_research_agent.py` の型）。
   外部サービスと claude は偽物に差し替える
8. skill を持たせるなら `plugin/<agent>/` を作り、`Config.agent_plugin_dir` の一覧（`AGENT_PLUGINS`）に足す
9. この文書の表と `README.md` のファイル一覧に足す

## 6. いまあるスキル

| エージェント | スキル | 中身 |
|---|---|---|
| 大学 | `sync-assignments` / `list-due` / `list-classes` / `time-report` / `ask` / `managing-academic-record` | Moodle の締切、授業ホームの成績・GPA・単位要件を扱う |
| 研究 | `run-claude` / `submit-job` / `list-jobs` / `cancel-job` / `forget-job` / `managing-wandb` | Claude/Codexの実行、pueue、W&Bのrun・artifact・metricsの読み取りと研究ログへの投影 |
| 仕事 | `list-events` / `ask` | Outlook の予定（JSON）／メール・SharePoint・Teams を読んで要点で答える |

## 7. エージェントに持たせないもの

- Slack の受け口（受けるのは本体だけ。エージェントは Slack に直接投稿しない）
- 依頼者への約束（上限で待つ、やり直す、通知する）
- スレッドと会話の単位（`session_id` は本体の SQLite が持ち、エージェントには渡すだけ）
- ほかのドメインの秘密情報
- Kei Agent 自身のコードを直す作業（本体の `self_fix.py` に残す）
- ジョブが「どのスレッドのものか」の記録（本体の SQLite。エージェントが持つのは待ち行列だけ）
