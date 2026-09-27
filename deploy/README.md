# 入れ方と運用

- はじめてなら `uv run kei-agent setup` の質問に答える。下は、setup がしていることと、手でするときの手順
- 使い方は [docs/using.md](../docs/using.md)、仕組みは [docs/architecture.md](../docs/architecture.md)

## 1. Slack App

1. 自分用のワークスペースに、チャンネルを作る（`#00_kei-agent`・`#01_overview`・`#10_<テーマ>`・`#20_course`・`#30_work`・`#40_knowledge`）
2. <https://api.slack.com/apps> → **Create New App** → **From a manifest** に、`uv run kei-agent manifest` の出力を貼る
3. **Install App** で入れ、Bot User OAuth Token（`xoxb-`）を控える
4. **Basic Information** → **App-Level Tokens** で、scope `connections:write` のトークン（`xapp-`）を作る
5. **App icon** に `slack/icon.png` を上げ、**Agents** の **Agent experience** をオンにして入れ直す
6. 自分のメンバー ID（`U…`）を控え、Kei Agent を各チャンネルに招く

- モジュールを足し外ししたら、`kei-agent manifest` の出力を **App Manifest** に貼り直し、**Install App** で入れ直す

## 2. 設定

```zsh
mkdir -p ~/.config/kei-agent/secrets && chmod 700 ~/.config/kei-agent/secrets
cp config.example.toml ~/.config/kei-agent/config.toml
cp profile.example.md ~/.config/kei-agent/profile.md
```

| ファイル | 中身 |
|---|---|
| `config.toml` | 使うモジュール・チャンネル・Notion のホーム・時刻など。場所は `KEI_AGENT_HOME`・`KEI_AGENT_CONFIG` で変えられる |
| `profile.md` | 話し方・所属・興味。会話する担当の指示書に足す |
| `prompts/<名前>` | 指示書を丸ごと差し替えるとき |

## 3. 秘密情報

- 置き場所は `~/.config/kei-agent/secrets/`（`[paths] secrets` で変えられる）。ファイルは `chmod 600`、Git に入れない
- 要る鍵は、本体のものと、オンにしたモジュールの `module.toml` の `[secrets]`（setup が聞き、doctor が確かめる）

`kei-agent.zsh`（どのプロセスも読む）:

```zsh
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export KEI_AGENT_ALLOWED_USER_ID="U..."
export KEI_AGENT_A2A_TOKEN="..."              # openssl rand -hex 32 で一度だけ作って貼る
export NOTION_TOKEN="ntn_..."                 # コネクト「Kei Agent」。読むのはゲートウェイだけ
export KEI_AGENT_NOTION_GATEWAY_TOKEN="..."   # ゲートウェイの親の合言葉。openssl rand -hex 32 で一度だけ作って貼る
# 任意
export TOGGL_API_TOKEN="toggl_sk_..."
export TOGGL_ORGANIZATION_ID="..."            # focus.toggl.com/<組織>/workspaces/<ワークスペース>/ の URL から
export TOGGL_WORKSPACE_ID="..."
```

- 合言葉2つは、作るコマンドではなく値を貼る。全プロセスが同じ値を読む（作り直すとつながらない）
- `NOTION_TOKEN` を持つのはゲートウェイだけ。ほかの起動スクリプトは読んだあとで消す

### 担当ごとの秘密情報

- `kei-agent-<名前>.zsh` は、共通のファイルのあとに、その担当のプロセスだけが読む（無くてもよい）

| ファイル | 中身 |
|---|---|
| `kei-agent-course.zsh` | `MOODLE_ICS_URL`、`unset CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CONFIG_DIR="$HOME/.claude-personal"`（Box をつないだ個人アカウント） |
| `kei-agent-work.zsh` | `unset CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CONFIG_DIR="$HOME/.claude-work"`（Microsoft 365 をつないだ会社アカウント） |
| `kei-agent-research.zsh` | 任意で `S2_API_KEY`（Semantic Scholar） |
| `kei-agent-voice.zsh` | 任意で `OPENAI_API_KEY`（マイクの会話）、`KEI_AGENT_REALTIME_VOICE`、`KEI_AGENT_MIC`、`KEI_AGENT_STACKCHAN_URL` |

プロファイルは一度作ってログインしておく:

```zsh
CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude   # /login → 個人アカウント。claude.ai で Box をつなぐ
CLAUDE_CONFIG_DIR=$HOME/.claude-work claude       # /login → 会社アカウント。claude.ai で Microsoft 365 をつなぐ
```

- Codex の連携は、ChatGPT のログイン（`~/.codex`）の Codex アプリでつなぐ（Box、Outlook Email、Outlook Calendar）

## 4. 常駐（launchd）

- リポジトリは保護フォルダ（書類・デスクトップ・ダウンロード）の外に置く（例 `~/src/kei-agent`）
- 研究テーマに保護フォルダを使うなら、`allow_protected_folders = true` と、仮想環境の Python へのフルディスクアクセスの許可が要る

```zsh
deploy/install.sh notion      # Notion のゲートウェイ（Notion を使うものより先に）
deploy/install.sh course      # 担当はどれも名前で（research・work・knowledge・voice も同じ）
deploy/install.sh             # 本体
deploy/install.sh course remove    # 登録を外す（本体は deploy/install.sh remove）
deploy/install.sh course print     # 登録する中身を見るだけ
```

| もの | 場所 |
|---|---|
| 状態 | `~/.local/state/kei-agent/`（`kei-agent.db`・`notion.json`・`notion-course.json`） |
| ログ | `~/Library/Logs/kei-agent/kei-agent.log`（5MB × 5世代）、起動の失敗は `launchd.log`、担当は `<名前>-launchd.log` |
| ジョブ | `pueue status --group kei-agent` |

| 反映のしかた | 中身 |
|---|---|
| `deploy/update.sh` | main で書きかけが無いときだけ動く。依存をそろえ、変わった plist だけ登録し直し、全部を起動し直して版を確かめる。`--pull` で先に origin から取り込む。push はしない |
| `deploy/restart-all.sh` | 起動し直すだけ（ゲートウェイ → 担当 → 本体の順） |

- 本体は起動したときに担当の版を見比べ、古い担当を起動し直す。取り込んだのに1時間たっても起動し直していなければ知らせる

## 5. Notion（最初の1回）

1. Notion でコネクト「Kei Agent」を作り、トークンを `NOTION_TOKEN` に貼る
2. 共通ホーム・研究ホーム・授業ホームを、どれもこのコネクトに共有する
3. 3つのページ ID を `config.toml` の `[notion]`（`hub_home`・`research_home`・`course_home`）に書く
4. ゲートウェイを動かす（`deploy/install.sh notion`）
5. DB を作る。どれも `--apply` を付けるまでは、作るものを並べるだけ

```zsh
uv run kei-agent-hub-setup --apply        # 共通ホーム
uv run kei-agent-notion-setup --apply     # 研究ホーム（ノートのテンプレートだけは Notion の画面で作る）
uv run kei-agent-module course setup --seed ~/.config/kei-agent/courses.toml   # 授業ホーム
```

- 授業ホームのほかのコマンドは [course-agent.md](../docs/agents/course-agent.md#コマンド)

## 6. そのほかの最初の1回

```zsh
uv run kei-agent-module time cards                  # 10_/20_/30_ に時間記録カードを置く（そのあと Slack で固定する）
deploy/backup-init.sh                               # ~/research を非公開リポジトリ research-data にする
sudo pmset repeat wakeorpoweron MTWRFSU 23:55:00    # 00:00 の夜間 Task のために毎晩 Mac を起こす
ffmpeg -f avfoundation -i ":default" -t 1 -f null - # マイクの許可を先に手で通す
gh auth status                                      # 要望を GitHub issue にするのに使う
```

## 7. 日々の運用

| したいこと | コマンド |
|---|---|
| 点検（読むだけ。`--all` で全部） | `uv run kei-agent doctor` |
| モジュールの一覧・足す・外す | `uv run kei-agent module list` / `add <名前>` / `remove <名前>`（`--dry-run` で見るだけ） |
| モジュールを作る・試す | `uv run kei-agent module new <名前>` / `test <名前>`（[docs/modules.md](../docs/modules.md)） |
| 定期処理を今すぐ1回 | `uv run kei-agent-schedule <名前>`（`--record` を付けなければ記録に残らない） |
| Slack の外から依頼を置く | `uv run kei-agent-ask --theme <テーマ> "〜して"`（`--note` で記録だけ） |

- `module add` / `remove` は `config.toml` の `modules` の行だけを書き換え、前のものを `config.toml.bak` に残す
- 22:00 の保守: 研究のセッションの記録を90日で消し、使い終わった worktree を消し、`~/research` と `~/kei-agent` を非公開リポジトリに push する（50MB を超えるファイルは外す）
- 別の Mac に移す: `research-data` を `~/research` に clone し、`sqlite3 ~/.local/state/kei-agent/kei-agent.db < ~/kei-agent/state/kei-agent.sql`

## 8. 困ったとき

| 症状 | 見るところ |
|---|---|
| 起動しない | `launchctl print gui/$(id -u)/com.kei-agent.assistant`、`launchd.log`。秘密情報のファイルが無いと止まる |
| 返事が来ない | App Home で AI が選ばれているか。`kei-agent.log` |
| 担当だけ失敗する | `curl -s http://127.0.0.1:<番地>/.well-known/agent-card.json`、`<名前>-launchd.log`、`KEI_AGENT_A2A_TOKEN` が全部で同じか |
| `can't open input file` | plist が古い起動スクリプトを指している。`deploy/install.sh <名前>` で登録し直す |
| Notion がつながらない | `curl -s http://127.0.0.1:8791/health`、`notion-launchd.log`、`KEI_AGENT_NOTION_GATEWAY_TOKEN` が全部で同じか |
| Notion で `can't reach` | ホームの外を触ろうとしている。ホームがコネクトに共有されているか、`[notion]` が合っているか |
| 大学・仕事の連携が見えない | Claude は担当のファイルの `CLAUDE_CONFIG_DIR` とそのログイン、Codex は Codex アプリの連携 |
| 声が出ない・聞かない | `ffmpeg`、マイクの許可、App Home のスイッチ、`voice-launchd.log` |
| 上限に当たった | 何もしなくてよい。明けてから自動でやり直す |
| 自己改善のあと起動しない | 3回失敗すると `deploy/run.sh` が `git revert` して前の版で起動し、Slack で知らせる |
