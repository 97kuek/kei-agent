# エージェントを増やすときの決まり

Kei Agent は「オーケストレーター＋ドメインごとのエージェント」の形で増えていく（構成は `architecture.svg`、
決めた経緯は `plan.md` の15章）。エージェントが増えるたびに形がずれると、どこに何があるか分からなくなる。
**この文書は参考ではなく、守る決まり**。外れているものを見つけたら、直す。

## 1. 層

| 層 | いまあるもの | LLM | 役目 |
|---|---|---|---|
| 判断 | Kei Agent 本体（`src/kei_agent/`） | 持つ | Slack の受け口、意図の判定、返事の組み立て、柵、Notion（研究）、決まった時刻の処理、上限の管理 |
| 実行 | 研究エージェント（`src/kei_agent_research/`） | 動かすが考えない | 渡された依頼文で `claude -p` を1回動かし、経過と結果を返す |
| 道具＋自分の判断 | 大学エージェント（`src/kei_agent_course/`） | 自分の claude を持つ | ドメインの外部サービス（Moodle・Box・Toggl・Notion の授業）を触り、そのドメインの質問に答える |

**ドメイン1つ＝エージェント1つ。** 研究・大学・仕事（予定）。ドメインの中をさらに割らない。

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

`src/kei_agent_a2a/claude.py` の `run()` を使う。自分で `claude` を起動する処理を書かない。

- 柵は `config.toml` から組む（`kei_agent.guard`）。接続先は**そのドメインに要るものだけ**を `Workspace.allowed_domains` に渡す
- 指示書は `prompts/<agent>.md`（`Workspace.system_prompt`）。学期ごとに変わる前提は作業場の `CLAUDE.md` に置く
- 上限時間は `Workspace.timeout_minutes`（大学は5分、研究は30分）
- MCP は `claude.mcp_config()` で **sandbox から読めない場所**（`<state_dir>/secrets/`）に書き、使い終わったら消す。
  トークンは claude に読ませない
- 書き込みできる鍵を claude に渡さない（Notion は読み取り専用のコネクトを渡し、書き込みは Python 側が行う）

## 5. 増やすときの手順

1. `src/kei_agent_<agent>/` に `card.py`（名刺）、`executor.py`（仕事）、`app.py`（`kei_agent_a2a.server.serve` を呼ぶだけ）
2. `pyproject.toml` に `[project.scripts]`、`[dependency-groups]`、`module-name` を足す
3. `config.toml` の `[a2a.agents]` に住所を足す
4. `deploy/run-<agent>.sh` と `deploy/com.kei-agent.<agent>.plist.template` を作り、`deploy/install.sh` の `case` に名前を足す
5. 秘密情報を `~/.config/zsh/local/kei-agent-<agent>.zsh` に置く（600）
6. 本体側の取り次ぎ（`src/kei_agent/<agent>.py`）と、Slack のチャンネル（`config.toml` の `[channels]`）
7. テストは**本物のサーバーを立てて往復を見る**（`tests/test_a2a.py`、`tests/test_research_agent.py` の型）。
   外部サービスと claude は偽物に差し替える
8. この文書の表と `README.md` のファイル一覧に足す

## 6. エージェントに持たせないもの

- Slack の受け口（受けるのは本体だけ。エージェントは Slack に直接投稿しない）
- 依頼者への約束（上限で待つ、やり直す、通知する）
- スレッドと会話の単位（`session_id` は本体の SQLite が持ち、エージェントには渡すだけ）
- ほかのドメインの秘密情報
- Kei Agent 自身のコードを直す作業（本体の `self_fix.py` に残す）
