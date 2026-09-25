# 入れ方と運用

使い方は [`docs/using.md`](../docs/using.md)、仕組みは [`docs/architecture.md`](../docs/architecture.md)。

## 1. Slack

1. 個人用のワークスペースを作り、チャンネルを作る（`#00_kei-agent`、`#01_overview`、`#02_research-strategy`、`#10_<テーマ>`、`#20_course`、`#30_work`）。番号を外した名前を `config.toml` の `[channels]` と合わせる
2. <https://api.slack.com/apps> → **Create New App** → **From a manifest** で `slack/manifest.yaml` を貼る
3. **Install App** で入れ、**Bot User OAuth Token**（`xoxb-`）を控える
4. **Basic Information** → **App-Level Tokens** で scope `connections:write` のトークン（`xapp-`）を作る
5. **Display Information** → **App icon** に `slack/icon.png` を上げる（アイコンは Git に入れていない）
6. **Agents** の **Agent experience** をオンにして入れ直す（入力欄の下の経過表示と、流しながらの返事に使う）
7. 自分のメンバー ID（`U…`）を控える
8. Kei Agent を各チャンネルに招待する

マニフェストを変えたときは **App Manifest** の画面に貼り直し、権限が変わったら **Install App** で入れ直す。`/toggl` コマンド（`commands` の scope と slash command）を足したマニフェストに更新したら、一度入れ直すまで `/toggl` は使えない。

## 2. 秘密情報

`~/.config/zsh/local/` に置き、`chmod 600` にする（Git に入れない）。値はここに書かない。

**`kei-agent.zsh`（共通。全プロセスが読む）**

```zsh
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export KEI_AGENT_ALLOWED_USER_ID="U..."
export KEI_AGENT_A2A_TOKEN="..."              # openssl rand -hex 32 で一度だけ作って貼る
export NOTION_TOKEN="ntn_..."                 # コネクト「Kei Agent」
export KEI_AGENT_NOTION_GATEWAY_TOKEN="..."   # openssl rand -hex 32 で一度だけ作って貼る
# 任意
export TOGGL_API_TOKEN="toggl_sk_..."
export TOGGL_ORGANIZATION_ID="..."
export TOGGL_WORKSPACE_ID="..."             # Toggl が無ければ時間は Notion にだけ書く
# export CLAUDE_CODE_OAUTH_TOKEN="..."        # 手元と別の Claude アカウントで動かすとき（claude setup-token）
# export S2_API_KEY="..."                     # Semantic Scholar
```

- `KEI_AGENT_A2A_TOKEN` と `KEI_AGENT_NOTION_GATEWAY_TOKEN` は、生成したコマンドではなく値をファイルに貼る。全プロセスが同じ値を読む必要があり、作り直すとつながらなくなる
- Toggl の ID は `focus.toggl.com/<組織>/workspaces/<ワークスペース>/` の URL から写す

**エージェントごとのファイル**（そのエージェントの起動スクリプトだけが読む）

| ファイル | 中身 |
|---|---|
| `kei-agent-course.zsh` | `MOODLE_ICS_URL`、`NOTION_COURSE_TOKEN="ntn_..."`、`unset CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CONFIG_DIR="$HOME/.claude-personal"` |
| `kei-agent-work.zsh` | `unset CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CONFIG_DIR="$HOME/.claude-work"` |
| `kei-agent-voice.zsh` | `OPENAI_API_KEY`、任意で `KEI_AGENT_REALTIME_VOICE`、`KEI_AGENT_MIC`（例 `":1"`）、`KEI_AGENT_STACKCHAN_URL` |

アカウント連携はログイン（プロファイル）に付いてくるので、大学と仕事は専用のプロファイルを作ってログインしておく。`claude setup-token` のトークンでは連携は使えない。

```zsh
CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude   # /login → 個人アカウント。claude.ai で Box と Notion をつなぐ
CLAUDE_CONFIG_DIR=$HOME/.claude-work claude       # /login → 会社アカウント（Microsoft 365）
```

個人プロファイルの Notion 連携には授業ホームだけを共有する。Codex を選ぶ actor は、Codex CLI のログインと `codex mcp list --json` に必要な connector が出ていることを確かめる。

## 3. 手元で起動して確かめる

```zsh
brew install pueue ffmpeg && brew services start pueue
uv sync --all-groups
source ~/.config/zsh/local/kei-agent.zsh
uv run kei-agent
```

- ログに `Kei Agent を起動しました` が出て、Slack でオンラインになる
- App Home で各 actor の provider（Claude / Codex）を選ぶ。選ぶまでその actor は動かない
- テーマのチャンネルで `@Kei Agent このディレクトリの中身を教えて` にスレッドで返事が来る

## 4. 常駐（launchd）

launchd のプロセスは `~/Documents`・`~/Desktop`・`~/Downloads` を読めないので、リポジトリはその外（例 `~/src/kei-agent`）に置く。

```zsh
deploy/install.sh                  # 本体
deploy/install.sh course           # 127.0.0.1:8787
deploy/install.sh research         # 127.0.0.1:8788
deploy/install.sh work             # 127.0.0.1:8789
deploy/install.sh voice            # 127.0.0.1:8790
deploy/install.sh notion-gateway   # 127.0.0.1:8791（先に kei-agent-notion-setup を済ませる）
deploy/install.sh remove           # 本体の登録を外す（エージェントは deploy/install.sh course remove など）
```

| もの | 場所 |
|---|---|
| 設定 | `config.toml`（`KEI_AGENT_CONFIG` で別のファイルを指せる） |
| 状態 | `~/.local/state/kei-agent/`（`kei-agent.db`、`notion.json`、`notion-course.json`） |
| ログ | `~/Library/Logs/kei-agent/kei-agent.log`（5MB × 5世代）、起動の失敗は `launchd.log`、エージェントは `<名前>-launchd.log` |
| ジョブ | `pueue status --group kei-agent` |

コードを入れ替えたら、エージェントも起動し直す（`launchctl kickstart -k gui/$(id -u)/com.kei-agent.<名前>`）。

## 5. Notion（最初の1回）

**研究ホーム**

1. Notion でコネクト「Kei Agent」（アクセストークン方式）を作り、トークンを `NOTION_TOKEN` に貼る
2. 空のページ「研究ホーム」を作り、コネクトに共有する
3. 実行する（何度実行しても重複しない）。ノートのテンプレートだけは Notion の画面で空の枠を作る

```zsh
uv run kei-agent-notion-setup <研究ホームのページID>
```

**共通ホーム**（`Keitaro Ueki`）: 同じコネクトに親ページを共有し、dry-run を確認してから反映する。

```zsh
uv run kei-agent-hub-setup
uv run kei-agent-hub-setup --apply
```

旧 Daily／振り返り（研究の「ノート」）を「日別記録」へ移すのは、一度だけの作業。dry-run が作る manifest（Git に入れない）で件数・同日の重複・本文を確かめ、その件数を渡して反映する。旧ページは消さない。

```zsh
uv run kei-agent-hub-migrate
uv run kei-agent-hub-migrate --apply --expected-count <確かめた件数>
```

**授業ホーム**

1. 授業用のコネクトを作り、トークンを `NOTION_COURSE_TOKEN` に貼る。「授業ホーム」をそのコネクトに共有する
2. 6つの DB をそろえる（`--seed <年度>` で `notion_setup.py` の履修科目を入れる）

```zsh
source ~/.config/zsh/local/kei-agent-course.zsh
uv run --group course kei-agent-course-setup <授業ホームのページID> --seed 2026
uv run --group course kei-agent-course-sync            # 手で締切を取り込む（--all で履修外も）
uv run --group course kei-agent-course-inspect         # Moodle と「授業」を読むだけで照合する
uv run --group course kei-agent-course-academic-import --dry-run <grades.html> <credits.html>
uv run --group course kei-agent-course-academic-import --apply <grades.html> <credits.html>   # --delete-inputs で入力を消す
```

## 6. そのほかの最初の1回

```zsh
uv run kei-agent-time-cards                            # 10_/20_/30_ に時間記録カードを投稿する。投稿後、Slack で各カードを手で固定する
deploy/backup-init.sh                                  # ~/research を非公開リポジトリ research-data にして最初の push をする
sudo pmset repeat wakeorpoweron MTWRFSU 23:55:00       # 00:00 の夜間 Task のために毎晩 Mac を起こす（やめるときは sudo pmset repeat cancel）
ffmpeg -f avfoundation -i ":default" -t 1 -f null -    # マイクの許可を先に手で通す（launchd からだと無音になることがある）
```

## 7. 日々の運用

- 定期処理を今すぐ1回: `uv run kei-agent-schedule <night|literature|daily|review|maintenance>`（`--record` を付けなければ今日の本番に影響しない）
- 声を通さず依頼を渡す: `uv run kei-agent-ask --theme <テーマ> "〜して"`（`--note` で記録だけ）
- 22:00 の保守は、Daily・Retro の材料（`overview/.kei-agent/digest/`）を30日、`~/research` の下のセッションの記録を90日で消し、使い終わった worktree を消してから、`~/research` → `research-data`、`~/kei-agent` → もう1つの非公開リポジトリに push する（`~/kei-agent` は自分で Git にして remote を付けておく。Git でなければ研究側だけ保存し、その旨を結果に出す）。50MB を超えるファイルはコミットから外す
- 別の Mac に移すときは、`research-data` を `~/research` に clone し、`sqlite3 ~/.local/state/kei-agent/kei-agent.db < ~/kei-agent/state/kei-agent.sql` で状態を戻す

## 8. 困ったとき

| 症状 | 見るところ |
|---|---|
| 起動しない | `launchctl print gui/$(id -u)/com.kei-agent.assistant \| grep -E 'state\|last exit'`、`launchd.log`。秘密情報のファイルがないと起動スクリプトが止まる |
| 返事が来ない | App Home で provider が選ばれているか。`kei-agent.log` |
| 大学・研究・仕事だけ失敗する | `curl -s http://127.0.0.1:8787/.well-known/agent-card.json`（ポートを替えて）と `<名前>-launchd.log`。`KEI_AGENT_A2A_TOKEN` が全プロセスで同じか |
| 研究で Notion がつながらない | `curl -s http://127.0.0.1:8791/health`、`notion-gateway-launchd.log`、`KEI_AGENT_NOTION_GATEWAY_TOKEN` |
| 大学・仕事の連携が見えない | エージェントのファイルの `unset CLAUDE_CODE_OAUTH_TOKEN` と `CLAUDE_CONFIG_DIR`、そのプロファイルでのログイン |
| 声が出ない・聞かない | `ffmpeg` があるか、マイクの許可、`OPENAI_API_KEY`、App Home のスイッチ、`voice-launchd.log` |
| 上限に当たった | 何もしなくてよい。明ける時刻がスレッドに出て、明けてから自動でやり直す |
| 自己改善のあと起動しない | 3回失敗すると `deploy/run.sh` が取り込んだ分を `git revert` して前の版で起動し、`update-rolled-back` を残して Slack で知らせる |
