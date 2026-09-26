# 入れ方と運用

使い方は [`docs/using.md`](../docs/using.md)、仕組みは [`docs/architecture.md`](../docs/architecture.md)。

## 1. Slack

1. 個人用のワークスペースを作り、チャンネルを作る（`#00_kei-agent`、`#01_overview`、`#10_<テーマ>`、`#20_course`、`#30_work`、`#40_knowledge`）。番号を外した名前を `config.toml` の `[channels]` と合わせる
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
export NOTION_TOKEN="ntn_..."                 # コネクト「Kei Agent」。読むのは Notion ゲートウェイだけ
export KEI_AGENT_NOTION_GATEWAY_TOKEN="..."   # ゲートウェイの親の合言葉。openssl rand -hex 32 で一度だけ作って貼る
# 任意
export TOGGL_API_TOKEN="toggl_sk_..."
export TOGGL_ORGANIZATION_ID="..."
export TOGGL_WORKSPACE_ID="..."             # Toggl が無ければ時間は Notion にだけ書く
# export CLAUDE_CODE_OAUTH_TOKEN="..."        # 手元と別の Claude アカウントで動かすとき（claude setup-token）
# export S2_API_KEY="..."                     # Semantic Scholar
```

- `KEI_AGENT_A2A_TOKEN` と `KEI_AGENT_NOTION_GATEWAY_TOKEN` は、生成したコマンドではなく値をファイルに貼る。全プロセスが同じ値を読む必要があり、作り直すとつながらなくなる
- Notion の鍵（`NOTION_TOKEN`）を持つのはゲートウェイだけ。ほかの起動スクリプトは読んだあとで消し、親の合言葉から作った client ごとの合言葉でゲートウェイを通す。LLM の子プロセスには親の合言葉も渡さない
- Toggl の ID は `focus.toggl.com/<組織>/workspaces/<ワークスペース>/` の URL から写す

**エージェントごとのファイル `kei-agent-<名前>.zsh`**（どれも任意。書き方は共通のファイルと同じ）

研究・大学・仕事・知識はどれも `deploy/run-agent.sh <名前>` で起動し、共通のファイルのあとに自分の名前のファイルだけを読む（無ければ共通のものだけで動く。同じ変数は上書きされる）。そのエージェントだけが要るものはここに置く。アカウント連携を使うエージェントは、連携を付けたアカウントのプロファイルを `CLAUDE_CONFIG_DIR` で選び、共通の `CLAUDE_CODE_OAUTH_TOKEN` を外す（`claude setup-token` のトークンでは連携は使えない）。

| ファイル | 中身 |
|---|---|
| `kei-agent-research.zsh` | なし（置かなくてよい） |
| `kei-agent-course.zsh` | `MOODLE_ICS_URL`、`unset CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CONFIG_DIR="$HOME/.claude-personal"`（個人アカウント。Box） |
| `kei-agent-work.zsh` | `unset CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CONFIG_DIR="$HOME/.claude-work"`（会社アカウント。Microsoft 365） |
| `kei-agent-knowledge.zsh` | なし（置かなくてよい。外の記事を読む担当なので、鍵は足さない） |
| `kei-agent-voice.zsh` | 声のレイヤ（`deploy/run-agent.sh voice`）が同じ規則で読む。`OPENAI_API_KEY`、任意で `KEI_AGENT_REALTIME_VOICE`、`KEI_AGENT_MIC`（例 `":1"`）、`KEI_AGENT_STACKCHAN_URL` |

プロファイルは一度作ってログインしておく。

```zsh
CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude   # /login → 個人アカウント。claude.ai で Box をつなぐ
CLAUDE_CONFIG_DIR=$HOME/.claude-work claude       # /login → 会社アカウント。claude.ai で Microsoft 365 をつなぐ
```

Codex のアカウント連携は、Codex の ChatGPT ログイン（`~/.codex`。どのエージェントも同じ1つ）に付いてくる。Codex を選ぶ担当があるなら、Codex アプリで Box、Microsoft Outlook Email、Microsoft Outlook Calendar をつないでおく（Codex の仕事は Outlook だけを読む。Teams・SharePoint を読むのは Claude のとき）。

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
deploy/install.sh knowledge        # 127.0.0.1:8792
deploy/install.sh voice            # 127.0.0.1:8790
deploy/install.sh notion-gateway   # 127.0.0.1:8791（Notion を使うものより先に。setup の CLI もここを通る）
deploy/install.sh remove           # 本体の登録を外す（エージェントは deploy/install.sh course remove など）
```

| もの | 場所 |
|---|---|
| 設定 | `config.toml`（`KEI_AGENT_CONFIG` で別のファイルを指せる） |
| 状態 | `~/.local/state/kei-agent/`（`kei-agent.db`、`notion.json`、`notion-course.json`） |
| ログ | `~/Library/Logs/kei-agent/kei-agent.log`（5MB × 5世代）、起動の失敗は `launchd.log`、エージェントは `<名前>-launchd.log` |
| 声からの問い合わせ口 | 本体が `127.0.0.1:8786`（`config.toml` の `[a2a] orchestrator`）で待ち受ける。`curl -s http://127.0.0.1:8786/.well-known/agent-card.json` |
| ジョブ | `pueue status --group kei-agent` |

main に取り込んだら、`deploy/update.sh` で反映する（先に origin から取り込むときは `--pull`）。main で書きかけが無いときだけ動き、依存をそろえ、plist が変わったものだけ登録し直し、全部を起動し直して、7つのプロセスが新しい版で動いているか（担当と本体は名刺、ゲートウェイは `/health` の version）を確かめる。push はしない。

手で起動し直すだけなら `deploy/restart-all.sh`（ゲートウェイ → 担当 → 本体の順）。担当だけ古い版のまま残ると、古いコードが新しい設定を読めずに止まる。本体は起動したときに担当の版を見比べて、古い担当を起動し直し、それでも古ければ Slack で知らせる。取り込んだのに1時間たっても起動し直していなければ、それも知らせる。plist の雛形（`deploy/com.kei-agent.plist.template`。どのプロセスも同じ雛形から作る）が変わったら、`kickstart` では前の plist のまま動くので、`deploy/install.sh <名前>` で登録し直す。登録する中身は `deploy/install.sh <名前> print` で先に確かめられる。

## 5. Notion（最初の1回）

Notion に届くのはゲートウェイだけなので、下の setup もゲートウェイが動いていないと使えない。

1. Notion でコネクト「Kei Agent」（アクセストークン方式）を作り、トークンを `NOTION_TOKEN` に貼る
2. 共通ホーム（`Keitaro Ueki`）、研究ホーム、授業ホームを、どれもこのコネクトに共有する
3. 3つのページ ID を `config.toml` の `[notion]`（`hub_home` / `research_home` / `course_home`）に書く。ゲートウェイはこの下だけを通す
4. ゲートウェイを動かす（`deploy/install.sh notion-gateway`。手元なら別の端末で `uv run kei-agent-notion-gateway`）

**研究ホーム**: 作るもの・足すものを確かめてから反映する（何度実行しても重複しない）。ノートのテンプレートだけは Notion の画面で空の枠を作る

```zsh
uv run kei-agent-notion-setup
uv run kei-agent-notion-setup --apply
```

**共通ホーム**（`Keitaro Ueki`）: dry-run を確認してから反映する。

```zsh
uv run kei-agent-hub-setup
uv run kei-agent-hub-setup --apply
```

**授業ホーム**: 6つの DB をそろえる（`--seed <年度>` で `notion_setup.py` の履修科目を入れる）

```zsh
source ~/.config/zsh/local/kei-agent.zsh          # ゲートウェイの親の合言葉
source ~/.config/zsh/local/kei-agent-course.zsh   # MOODLE_ICS_URL
uv run --group course kei-agent-course-setup --seed 2026
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
gh auth status                                         # 要望を GitHub issue にするのに使う。ログインしていなければ gh auth login
```

## 7. 日々の運用

- 定期処理を今すぐ1回: `uv run kei-agent-schedule <night|literature|daily|review|maintenance>`（`--record` を付けなければ今日の本番に影響しない）
- 声を通さず依頼を渡す: `uv run kei-agent-ask --theme <テーマ> "〜して"`（`--note` で記録だけ）
- 22:00 の保守は、`~/research` の下のセッションの記録を90日で消し、Toggl のアプリで直接測った記録を時間記録に取り込み、使い終わった worktree を消してから、`~/research` → `research-data`、`~/kei-agent` → もう1つの非公開リポジトリに push する（`~/kei-agent` は自分で Git にして remote を付けておく。Git でなければ研究側だけ保存し、その旨を結果に出す）。50MB を超えるファイルはコミットから外す
- 別の Mac に移すときは、`research-data` を `~/research` に clone し、`sqlite3 ~/.local/state/kei-agent/kei-agent.db < ~/kei-agent/state/kei-agent.sql` で状態を戻す

## 8. 困ったとき

| 症状 | 見るところ |
|---|---|
| 起動しない | `launchctl print gui/$(id -u)/com.kei-agent.assistant \| grep -E 'state\|last exit'`、`launchd.log`。秘密情報のファイルがないと起動スクリプトが止まる |
| 返事が来ない | App Home で provider が選ばれているか。`kei-agent.log` |
| 大学・研究・仕事だけ失敗する | `curl -s http://127.0.0.1:8787/.well-known/agent-card.json`（ポートを替えて）と `<名前>-launchd.log`。`KEI_AGENT_A2A_TOKEN` が全プロセスで同じか |
| `<名前>-launchd.log` に `can't open input file` | 登録してある plist が、いまはない起動スクリプトを指している。`deploy/install.sh <名前>` で登録し直す |
| Notion がつながらない | `curl -s http://127.0.0.1:8791/health`、`notion-gateway-launchd.log`、`KEI_AGENT_NOTION_GATEWAY_TOKEN` が全プロセスで同じか |
| Notion で `can't reach` と断られる | そのホームの外を触ろうとしている。ホームがコネクト「Kei Agent」に共有されているか、`config.toml` の `[notion]` が合っているか |
| 大学・仕事の連携が見えない | Claude なら、エージェントのファイルの `unset CLAUDE_CODE_OAUTH_TOKEN` と `CLAUDE_CONFIG_DIR`、そのプロファイルでのログイン。Codex なら、ChatGPT のログインと Codex アプリの連携 |
| 声が出ない・聞かない | `ffmpeg` があるか、マイクの許可、`OPENAI_API_KEY`、App Home のスイッチ、`voice-launchd.log` |
| 上限に当たった | 何もしなくてよい。明ける時刻がスレッドに出て、明けてから自動でやり直す |
| 自己改善のあと起動しない | 3回失敗すると `deploy/run.sh` が取り込んだ分を `git revert` して前の版で起動し、`update-rolled-back` を残して Slack で知らせる |
