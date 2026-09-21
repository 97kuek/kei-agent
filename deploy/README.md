# Kei Agent のセットアップ

入れて動かすまでの手順。使い方は `docs/using.md`、仕組みは `docs/design.md`。

## 1. Slack のワークスペースとチャンネル

1. 個人用のワークスペースを作る
2. チャンネルを作る。名前は `config.toml` の `[channels]` と合わせる

   | 種類 | チャンネル名 | Kei Agent の動き |
   |---|---|---|
   | 研究全体・朝のまとめ | `#01_overview` | すべてのテーマを読むだけ。書き込みは `~/kei-agent/overview/`。Daily と振り返りもここに届く |
   | 中長期の方針 | `#research-strategy` | 同上 |
   | Kei Agent の改善 | `#00_kei-agent` | 要望を `~/kei-agent/overview/backlog.md` に記録し、案に同意すると Kei Agent が自分のコードを直す（9章）。Kei Agent がうまく動かなかったときの知らせもここに届く |
   | 研究テーマ | テーマの名前（例: `#vlm-counting`） | Kei Agent を招待すると、`~/research/<チャンネル名>/` で作業する。Notion のテーマの名前も同じ |
   | 大学 | `#course` | 授業と課題。claude -p は動かさず、大学エージェント（A2A）に取り次ぐ（7.5節） |

   Kei Agent を招待したチャンネルは、上の4つ以外すべて研究テーマとして扱う。個人用のチャンネルには Kei Agent を招待しない。

3. サイドバーは名前の順に並ぶので、`research-` のチャンネルはまとまる。朝に見る `#01_overview` にスターをつけると一番上に出る。サイドバーのカテゴリ（セクション）は Slack の有料プランでだけ使える

4. テーマを終えたら、チャンネルをアーカイブする。Kei Agent は Daily の材料や論文の新着で、そのテーマを見なくなる。作業用ディレクトリはバックアップに残る

## 2. Slack App「Kei Agent」

1. <https://api.slack.com/apps> → **Create New App** → **From a manifest** → ワークスペースを選び、`slack/manifest.yaml` の中身を貼る
2. **Install App** → ワークスペースにインストールし、**Bot User OAuth Token**（`xoxb-`）を控える
3. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**。scope に `connections:write` を付け、トークン（`xapp-`）を控える
4. **Basic Information** → **Display Information** → **App icon** に `slack/icon.png` をアップロードする（アイコンはマニフェストでは変えられない。似顔絵なので Git には入れず、手元にだけ置いている）
5. 自分のSlackユーザーIDを控える（Slack でプロフィール → ︙ → **メンバーIDをコピー**。`U` で始まる）
6. **Agents** の **Agent experience** をオンにし、アプリを入れ直す（Install App → Reinstall）。作業中の表示（「Working...」）と、返事を流しながら見せる表示に使う。有効にしなくても Kei Agent は動き、その場合は結果をまとめて投稿する

   `slack/manifest.yaml` には `features.agent_view` と、設定画面に使う `features.app_home`・`settings.interactivity` が入っているので、**App Manifest** の画面に貼り直せば有効になる。コマンドで入れ替えるなら、**Settings → App Configuration Tokens** で作ったトークン（`xoxe-`）を使う。

   ```zsh
   TOKEN="xoxe-..." APP_ID="A..."   # App ID は api.slack.com/apps のアプリの Basic Information にある
   python3 -c 'import json,sys,yaml; print(json.dumps({"manifest": yaml.safe_load(open("slack/manifest.yaml"))}))' \
     | curl -s -X POST https://slack.com/api/apps.manifest.update \
         -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
         --data @- --url-query app_id="$APP_ID" | python3 -m json.tool
   ```

   入れ替えたあとは **Install App** で入れ直す（権限が変わったとき）。

## 3. 秘密情報

`~/.config/zsh/local/kei-agent.zsh` を作る（Git に入れない）。

```zsh
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export KEI_AGENT_ALLOWED_USER_ID="U..."
# Notion のコネクト「Kei Agent」のアクセストークン（ないと Notion につながず、夜間の Task は動かない）
export NOTION_TOKEN="ntn_..."
# 任意: Toggl の API キーと宛先（研究時間の記録に使う。どれかがなければ人の時間は空欄になる）
# キーは Toggl の設定 →「Togglアカウント」→「APIトークン」。ID は Toggl を開いたときの URL
# （focus.toggl.com/<組織>/workspaces/<ワークスペース>/）から写す
export TOGGL_API_TOKEN="toggl_sk_..."
export TOGGL_ORGANIZATION_ID="..."
export TOGGL_WORKSPACE_ID="..."
# 任意: Kei Agent が claude を動かすときのログイン。書かなければ、ふだんのログイン（キーチェーン）を使う。
# `claude setup-token` で作ったトークンを入れると、手元の作業と別のアカウントで Kei Agent を動かせる
# （契約の上限を分けたいときに使う）
# export CLAUDE_CODE_OAUTH_TOKEN="..."
# 任意: Semantic Scholar の APIキー（なくても動くが、混雑時に 429 になりやすい）
# export S2_API_KEY="..."
```

`chmod 600` にしておく。`~/.zshrc` から `~/.config/zsh/local/*.zsh` を読んでいれば、ターミナルでもそのまま使える。

エージェントごとの秘密情報は、**別のファイルに分ける**（`docs/agents.md`）。こうすると、大学のトークンが
研究エージェントのプロセスに載らない。

```zsh
# ~/.config/zsh/local/kei-agent-course.zsh（大学エージェントだけが読む）
export MOODLE_ICS_URL="https://wsdmoodle.waseda.jp/calendar/export_execute.php?..."
export NOTION_COURSE_TOKEN="ntn_..."        # 授業と課題を書く（Python 側）
# claude が Box と Notion を読むぶんはアカウントの連携を使うので、鍵は要らない（7.7 章）
unset CLAUDE_CODE_OAUTH_TOKEN
export CLAUDE_CONFIG_DIR="$HOME/.claude-personal"
```

## 4. 手元で起動して確かめる

```zsh
brew install pueue && brew services start pueue   # 済んでいれば不要
uv sync
uv run kei-agent
```

確認すること（ステップ1〜3）:

- ログに `Kei Agent を起動しました` が出て、Slack で Kei Agent がオンラインになる
- テーマのチャンネルに Kei Agent を招待すると、`~/research/<チャンネル名>/` と `CLAUDE.md` ができる
- `@Kei Agent このディレクトリの中身を教えて` にスレッドで返信が来る。メンションなしのメッセージには反応しない

## 5. 常時起動（launchd）

launchd から起動したプロセスは、macOS の保護フォルダ（`~/Documents`、`~/Desktop`、`~/Downloads`）を読めない。
リポジトリをその外（例: `~/src/kei-agent`）に置いてから登録する。

```zsh
deploy/install.sh          # 登録して起動（ログイン時に起動し、落ちたら再起動する）
deploy/install.sh course   # 大学エージェント（A2A サーバー、127.0.0.1:8787）
deploy/install.sh research # 研究エージェント（A2A サーバー、127.0.0.1:8788）
deploy/install.sh work     # 仕事エージェント（A2A サーバー、127.0.0.1:8789）
deploy/install.sh remove   # 登録を外す（course / research / work も同じように remove を付ける）
tail -f ~/Library/Logs/kei-agent/kei-agent.log
launchctl print gui/$(id -u)/com.kei-agent.assistant | grep -E 'state|last exit'
```

`claude` は、ふだんのログイン（キーチェーン）で動く。launchd からログイン情報を読めないときや、
手元の作業と別のアカウント（別の契約の枠）で Kei Agent を動かしたいときは、`claude setup-token` で作った
トークンを `CLAUDE_CODE_OAUTH_TOKEN` として秘密情報のファイルに足す。

契約の上限に達したときは、Kei Agent がスレッドに「◯時◯分ごろに自動でやり直す」と書き、明けてから
止まった依頼を自分でやり直す。決まった時刻の処理も、上限の間は始めず、明けてから取りこぼしとして動かす。

## 6. 決まった時刻の処理

時刻は `config.toml` の `[schedule]` で変えられる。Mac がスリープしていて時刻を逃した処理は、起きたときに実行する（夜間の Task は12時間、それ以外は3時間以内）。

夜間（01:30）にも Task を進めたいときは、一度だけ次を実行して、毎晩 01:25 に Mac を起こす（電源につないでおく）。

```zsh
sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00
pmset -g sched   # 確認
sudo pmset repeat cancel   # やめるとき
```

今すぐ1回動かして確かめるときは、次を実行する（`--record` を付けなければ、今日の本番の実行には影響しない）。

```zsh
source ~/.config/zsh/local/kei-agent.zsh
uv run kei-agent-schedule daily        # night / literature / daily / review
```

テーマの先行研究を見張るには、テーマの `CLAUDE.md` の「## 検索キーワード」に、1行に1つ英語で書く。

## 7. Notion の研究ホーム

1. Notion の開発者ツール → コネクション → 新規コネクト（アクセストークン方式、名前 `Kei Agent`）を作り、トークンを秘密情報のファイルの `NOTION_TOKEN` に書く
2. Notion で空のページ「研究ホーム」を作り、コネクトの「コンテンツへのアクセス」にそのページを追加する
3. 次を実行する（何度実行しても重複しない）

```zsh
source ~/.config/zsh/local/kei-agent.zsh
uv run kei-agent-notion-setup <研究ホームのページID>
```

## 7.5 授業用の Notion（大学エージェント）

1. <https://www.notion.so/profile/integrations> で、授業用のコネクト（例: `Kei Agent（授業）`）を作り、トークンを控える
2. Notion に「授業ホーム」のページを作り、そのコネクトの「コンテンツへのアクセス」に追加する
3. 秘密情報のファイルに `export NOTION_COURSE_TOKEN="ntn_..."` を足す
4. 次を実行すると、「授業」「課題」の2つのデータベースができる（あとから実行しても、足りない項目だけ足す）

   ```zsh
   uv run --group course kei-agent-course-setup <授業ホームのページID> --seed
   ```

   「授業」は科目名・科目コード・学期・曜日・時限・Moodle・状態、「課題」は締切や状態と、科目へのリレーションを持つ。
   `--seed` を付けると、`notion_setup.py` の `AUTUMN_2026` に書いた履修科目を入れる（同じ名前があれば足さない）。
   研究用のコネクトとは分けてあるので、授業エージェントは研究のデータベースに触れない。

5. Slack で `#20_course` を作り、Kei Agent を招待する（`config.toml` の `[channels] course` に名前を書く）。
   このチャンネルの依頼は claude -p を動かさず、大学エージェントに取り次ぐ
6. 手で取り込みたいときは、次を実行する

   ```zsh
   uv run --group course kei-agent-course-sync          # 履修科目の締切だけを「課題」に入れる
   uv run --group course kei-agent-course-sync --all    # 履修していない科目の締切も入れる
   ```

   取り込むのは「授業」に入れた科目の締切だけ。Moodle のカレンダーには新入生向けの資料なども並ぶため、
   それらは入れずに、科目名だけを返事で知らせる。

## 7.55 仕事エージェントの Claude アカウント

仕事エージェントは、会社の Claude アカウントに付いている Microsoft 365 の連携（Outlook・
SharePoint・Teams）を使う。**連携はログインにしか付いてこない**（`claude setup-token` で作る
長期トークンでは使えない）ので、会社アカウント専用のプロファイルを1つ作る。

```zsh
CLAUDE_CONFIG_DIR=$HOME/.claude-work claude     # 起動して /login → 会社アカウント → 終了
```

そのうえで、秘密情報をこう分ける。

| ファイル | 中身 | 効き先 |
|---|---|---|
| `kei-agent.zsh`（共通） | `CLAUDE_CODE_OAUTH_TOKEN`（**個人**アカウントの setup-token） | 本体・研究・大学 |
| `kei-agent-work.zsh` | `unset CLAUDE_CODE_OAUTH_TOKEN` と `CLAUDE_CONFIG_DIR=$HOME/.claude-work` | 仕事だけ |

こうすると、端末のログインをどちらに切り替えても、仕事だけ会社の枠・会社の連携で動く。

## 7.6 研究エージェント（A2A）

研究の作業（`claude -p`）を別のプロセスで動かす。`config.toml` の `[a2a.agents]` から `research`
の行を外すと、今までどおり本体の中で動かす（具合が悪いときは、この1行で元に戻せる）。

```zsh
deploy/install.sh research
curl -s http://127.0.0.1:8788/.well-known/agent-card.json | python3 -m json.tool | head
tail -f ~/Library/Logs/kei-agent/research-launchd.log
```

Kei Agent 本体を入れ替えたときは、研究エージェントも入れ替える（同じリポジトリを読むので、
`launchctl kickstart -k gui/$(id -u)/com.kei-agent.research` で起動し直す）。

## 7.7 Box と Notion（学部要項・過去問・授業）

大学エージェントの claude は、**Claude のアカウントに付いている連携**で Box と Notion を読む
（自前のアプリは要らない）。連携はログインに付いてくるので、個人アカウント専用のプロファイルを1つ作る。

```zsh
CLAUDE_CONFIG_DIR=$HOME/.claude-personal claude    # 起動して /login → 個人アカウント → 終了
```

`kei-agent-course.zsh` に次を入れる（`unset` を忘れると、共通のトークンが効いて連携が見えない）。

```zsh
unset CLAUDE_CODE_OAUTH_TOKEN
export CLAUDE_CONFIG_DIR="$HOME/.claude-personal"
```

claude.ai の設定で Box と Notion の連携を繋いでおくこと。使わせるのは読む道具だけで、
書き込み・移動・アップロードは断る（`src/kei_agent_course/tools.py`）。

## 8. バックアップとログ

`~/research/` を非公開の GitHub リポジトリ（`research-data`）にし、毎晩 22:00 に Kei Agent がコミットして push する。
Kei Agent の状態（SQLite の中身を SQL にしたものと、Notion の ID）も `~/kei-agent/state/` に書き出して一緒に保存する。
50MB を超えるファイルは GitHub に置けないので、自動でコミットから外す。

```zsh
deploy/backup-init.sh              # 最初の1回だけ。非公開リポジトリを作って最初の push をする
uv run kei-agent-schedule maintenance   # 今すぐ整理とバックアップを1回動かす
```

別の Mac に移すときは、`research-data` を `~/research` に clone し、`state/kei-agent.sql` から状態を戻す（`sqlite3 ~/.local/state/kei-agent/kei-agent.db < ~/kei-agent/state/kei-agent.sql`）。

同じ 22:00 に、古いファイルを整理する（日数は `config.toml` の `[maintenance]`）。

| 整理するもの | 残す日数 |
|---|---|
| Daily と振り返りの材料（`overview/.kei-agent/digest/`） | 30日 |
| テーマのディレクトリで動かした Claude のセッションの記録（`~/.claude/projects/` のうち `~/research` の下に対応するものだけ） | 90日。消えたセッションのスレッドは、次に返信したときにスレッドの履歴から続きを始める |
| Kei Agent が自分を直すのに使った worktree（`<state_dir>/worktrees/`）と、案を考えるときの一時ディレクトリ | 直している最中のもの以外は毎晩消す |

ログは Kei Agent 自身が `~/Library/Logs/kei-agent/kei-agent.log` に書き、5MB ごとに回して5世代だけ残す。
`~/Library/Logs/kei-agent/launchd.log` には、起動に失敗したときの出力だけが残る。

## 9. 机の上の音声対話（`kei-agent-voice`）

相談相手は Codex、作業は Slack の Kei Agent（Claude）。docs/design.md の12章。

```zsh
# VOICEVOX アプリを立ち上げてから（立ち上げていなければ、声なしで文字だけ動く）
uv run kei-agent-voice
```

- 喋る入口は、いまのところ Aqua Voice などで「ターミナルに入力する」形
- Enter だけ押すと読み上げが止まる。「新しく話そう」で会話を切る。「じっくり考えて」を含めると、その回だけ深く考える
- 決まったことは Slack に残り、依頼は読み上げの確認を経て Kei Agent に渡る（`<state_dir>/asks/`）
- 声の全文は `~/kei-agent/overview/voice/<日付>.md` に残り、毎晩の保守で30日で消える
- 手でも渡せる: `uv run kei-agent-ask --theme amr-query "〜して"`、`--note` を付けると作業させず記録だけ

## 10. Kei Agent が自分を入れ替えるときの動き

`#00_kei-agent` から Kei Agent が自分のコードを直すと（docs/design.md の10章）、main に取り込んで push したあと、
動いている作業がなくなったところで自分で終了する。launchd の `KeepAlive` が新しい版で起動し直す。

- 取り込むときに `<state_dir>/update-pending` を置き、新しい版が Slack につながったら消す
- つながらないまま3回起動し直したら、`deploy/run.sh` が取り込んだ分を `git revert` して前の版で起動し、
  `<state_dir>/update-rolled-back` を残す。Kei Agent はそれを見て Slack に知らせ、取り消しを push する

## 状態の置き場所

| もの | 場所 |
|---|---|
| 設定 | `config.toml`（`KEI_AGENT_CONFIG` で別のファイルを指定できる） |
| スレッドとセッション、ジョブ、実行時間 | `~/.local/state/kei-agent/kei-agent.db` |
| テーマの作業用ディレクトリ | `~/research/` |
| ジョブ | `pueue status --group kei-agent` |
| ログ | `~/Library/Logs/kei-agent/kei-agent.log`（5MB ごとに回す）、起動の失敗は `launchd.log` |
| バックアップ | `~/research/.git` → GitHub の非公開リポジトリ `research-data` |
