# 入れ方と運用

- はじめてなら `uv run kei-agent setup` の質問に答える。下は、setup がしていることと、手でするときの手順
- 使い方は [docs/using.md](../docs/using.md)、仕組みは [docs/architecture.md](../docs/architecture.md)

## 1. Dot の接続

1. 自分用の Slack ワークスペースに、[チャンネル一覧](../docs/using.md#チャンネル) のチャンネルを作る
2. Dot の Slack 連携を接続し、使うチャンネルに Dot を招く。依頼者のメンバー ID（`U…`）を控える
3. 名前・通話・プラグイン・定期処理を [Dot 側の設定](../docs/dots.md#dot-側の設定) に合わせる

Mac の実行サービスは MCP で依頼を受け、通知を Outbox に保存する。Slack への投稿は Dot が担当する。

## 2. 設定

起動前に、既存の `config.toml` から `handoff_after_turns` と `[time]` の `prefixes`・`pick_course` を削除する。会話の引き継ぎは MCP の `handoff`、時間の開始・停止は MCP の `timer` を使う。

組み込み・利用者のモジュールの枠の版は `api = 2`。Dot からの操作は MCP に公開する。


```zsh
mkdir -p ~/.config/kei-agent/secrets && chmod 700 ~/.config/kei-agent/secrets
cp config.example.toml ~/.config/kei-agent/config.toml
cp agents.example.csv ~/.config/kei-agent/agents.csv
cp schedules.example.csv ~/.config/kei-agent/schedules.csv   # 時刻を変えないなら要らない
cp profile.example.md ~/.config/kei-agent/profile.md
```

| ファイル | 中身 |
|---|---|
| `config.toml` | 全体で1つの設定（状態の置き場所・同時に動かす数・sandbox・保守・秘密情報の置き場所・モジュールの設定）。場所は `KEI_AGENT_HOME`・`KEI_AGENT_CONFIG` で変えられる |
| `agents.csv` | 担当の表。モジュールのオンオフ・チャンネル・作業場・Notion のホーム・AI・アカウント（下の「担当の表」）。無ければ組み込みを全部使い、AI は未選択 |
| `schedules.csv` | 定期処理の表。時刻とオンオフ（下の「定期処理の表」）。無ければ既定の時刻 |
| `profile.md` | 話し方・所属・興味。会話する担当の指示書に足す |
| `prompts/<名前>` | 指示書を丸ごと差し替えるとき |

### 担当の表（`agents.csv`）

- `setup` は、選んだモジュールと AI でこの表を作る（例は `agents.example.csv`）
- 1行に1つ。表に無いモジュールはオフ。Excel や Numbers で開いてよい（UTF-8 で保存する）

| 列 | 書くこと |
|---|---|
| `module` | モジュールの名前。本体の行は `router`（振り分けの AI）と `overview`（研究全体のチャンネル） |
| `enabled` | `true` / `false` |
| `channels` | 番号を外したチャンネルの名前。複数は空白で区切る。空欄なら `module.toml` の既定。work の行は、`work-*` の形をプロジェクトのチャンネルに、ほかを `#3-work` の名前にする（例 `work work-*`。書かなかったほうは既定のまま） |
| `folder` | その担当の作業場。research の行はテーマのフォルダを置く場所（既定 `~/research`）、course の行は機械処理に使うフォルダ（既定 `~/course`）、work の行はプロジェクトの作業場を置く場所（既定 `~/work`）。AI を使う担当と course の行だけ。`/tmp` の下には置かない（sandbox が一時フォルダへの書き込みを許すため） |
| `engine` | `claude` / `codex`。空欄の担当は動かない |
| `notion` | その担当が届く Notion のホームのページ ID（URL の末尾32文字）。overview の行は共通ホーム、research は研究ホーム、course は授業ホーム。空欄ならその担当は Notion を使わない |
| `claude_account` / `codex_account` | その担当が使うアカウントのフォルダ（`CLAUDE_CONFIG_DIR`・`CODEX_HOME`。例 `~/.claude-work`）。空欄ならプロセスの既定のアカウント。Claude のフォルダを書いた担当では、共通の `CLAUDE_CODE_OAUTH_TOKEN` を外して動かす |
| `claude_email` | そのフォルダにログインしているはずの人（例 会社のメールアドレス）。書けば、Claude を動かす前に確かめ、違う人なら止めて `#0-kei-agent` に知らせる（ログインの入れ替えで会社と個人が混ざらないように）。Codex では確かめない |
| `model` / `effort` | ふだんは空欄（用途ごとに軽いモデルと重いモデルを選び分ける）。書くと、その担当の用途をすべてそのモデルにする（`manual = true` の明示の用途は除く）。使えるモデルは [architecture.md](../docs/architecture.md#actor-とモデル) |

- `config.toml` に `modules`・`[channels]`・`[agents]`（前の書き方）があると、起動しない（書く場所を1つにする）
- 変えたら `deploy/restart-all.sh` で起動し直す。書き間違いは起動と `kei-agent doctor` が行を示して知らせる
- AI とアカウントを変えるのは、この表だけ。変えたら `deploy/restart-all.sh` で起動し直す

### 定期処理の表（`schedules.csv`）

- 1行に1つの処理。列は `name,enabled,time`（例 `reading,true,07:00`）。`enabled` を `false` にすると、その処理を行わない
- 書ける名前は、本体の `night`・`daily`・`review`・`maintenance` と、モジュールの `module.toml` の `[schedules]`（`literature`・`reading`・`toggl_import` など）
- Dot の定期処理を使う場合は [停止対象](../docs/dots.md#kei-agent-側で止めるもの) を false にする。
- 書いていない処理は既定の時刻で動く。変えたら `deploy/restart-all.sh` で起動し直す

## 3. 秘密情報

- 置き場所は `~/.config/kei-agent/secrets/`（`[paths] secrets` で変えられる）。ファイルは `chmod 600`、Git に入れない
- 要る鍵は、本体のものと、オンにしたモジュールの `module.toml` の `[secrets]`（setup が聞き、doctor が確かめる）

`kei-agent.zsh`（どのプロセスも読む）:

```zsh
export KEI_AGENT_ALLOWED_USER_ID="U..."
export KEI_AGENT_A2A_TOKEN="..."              # openssl rand -hex 32 で一度だけ作って貼る
export KEI_AGENT_HANDS_TOKEN="..."            # MCP の合言葉。openssl rand -hex 32 で一度だけ作って貼る
export NOTION_TOKEN="ntn_..."                 # コネクト「Kei Agent」。読むのはゲートウェイだけ
export KEI_AGENT_NOTION_GATEWAY_TOKEN="..."   # ゲートウェイの親の合言葉。openssl rand -hex 32 で一度だけ作って貼る
# 任意
export TOGGL_API_TOKEN="toggl_sk_..."
export TOGGL_ORGANIZATION_ID="..."            # focus.toggl.com/<組織>/workspaces/<ワークスペース>/ の URL から
export TOGGL_WORKSPACE_ID="..."
```

- 合言葉は、作るコマンドではなく値を貼る。全プロセスが同じ値を読む（作り直すとつながらない）
- `NOTION_TOKEN` を持つのはゲートウェイだけ。ほかの起動スクリプトは読んだあとで消す

### 担当ごとの秘密情報

- `kei-agent-<名前>.zsh` は、共通のファイルのあとに、その担当のプロセスだけが読む（無くてもよい）

| ファイル | 中身 |
|---|---|
| `kei-agent-course.zsh` | `MOODLE_ICS_URL`、任意で `MOODLE_API_URL` / `MOODLE_API_TOKEN`（[提出状態同期](../docs/agents/course-agent.md#moodle-の提出受験終了の同期)） |
| `kei-agent-research.zsh` | 任意で `S2_API_KEY`（Semantic Scholar） |
| `kei-agent-voice.zsh` | 任意で `OPENAI_API_KEY`（マイクの会話）、`KEI_AGENT_REALTIME_VOICE`、`KEI_AGENT_MIC`、`KEI_AGENT_STACKCHAN_URL` |

ローカルの AI 担当のアカウント（仕事は Microsoft 365 をつないだ会社）は、`agents.csv` のその行の `claude_account`（と `codex_account`）にフォルダを書く。プロファイルは一度作ってログインしておく:

```zsh
CLAUDE_CONFIG_DIR=$HOME/.claude-work claude       # /login → 会社アカウント。claude.ai で Microsoft 365 をつなぐ
```

- Codex の連携は、ChatGPT のログイン（`~/.codex`）の Codex アプリでつなぐ（Outlook Email、Outlook Calendar）。大学の Box は Dot 側で接続する

## 4. 常駐（launchd）

- リポジトリは保護フォルダ（書類・デスクトップ・ダウンロード）の外に置く（例 `~/src/kei-agent`）
- 研究テーマに保護フォルダを使うなら、`allow_protected_folders = true` と、仮想環境の Python へのフルディスクアクセスの許可が要る

```zsh
deploy/install.sh notion      # Notion のゲートウェイ（Notion を使うものより先に）
deploy/install.sh course      # 担当はどれも名前で（research・work・voice も同じ）
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
3. 3つのページ ID を `agents.csv` の `notion` 列（overview・research・course の行）に書く
4. ゲートウェイを動かす（`deploy/install.sh notion`）
5. DB を作る。共通・研究ホームは `--apply` を付けるまでは作るものを並べるだけ。大学の setup は DB を作成する

```zsh
uv run kei-agent-hub-setup --apply        # 共通ホーム
uv run kei-agent-notion-setup --apply     # 研究ホーム（ノートのテンプレートだけは Notion の画面で作る）
uv run kei-agent-module course setup --seed ~/.config/kei-agent/courses.toml   # 授業ホーム
```

- 授業ホームのほかのコマンドは [course-agent.md](../docs/agents/course-agent.md#コマンド)

## 6. そのほかの最初の1回

```zsh
deploy/backup-init.sh                               # ~/research を非公開リポジトリ research-data にする
sudo pmset repeat wakeorpoweron MTWRFSU 23:55:00    # 00:00 の夜間 Task のために毎晩 Mac を起こす
ffmpeg -f avfoundation -i ":default" -t 1 -f null - # マイクの許可を先に手で通す
gh auth status                                      # 要望を GitHub issue にするのに使う
```

### MCP サーバー

Dot・Claude Code・Codex から、Mac の作業場に依頼する MCP サーバー。

1. `config.toml` に `[hands]` の `url = "http://127.0.0.1:8785"` を書き、`KEI_AGENT_HANDS_TOKEN` を置いて `deploy/restart-all.sh`
2. MCP クライアントに登録する（合言葉はシェルの環境変数から渡す。ファイルに書き写さない）。担当が使うアカウントのフォルダ（`agents.csv` の `claude_account`）の user の範囲には登録しない。アカウントの連携を使う担当（仕事）は user の MCP も読むので、担当からこの MCP サーバーが見えてしまう

```zsh
claude mcp add -s user --transport http kei-agent-hands http://127.0.0.1:8785/mcp \
  --header 'Authorization: Bearer ${KEI_AGENT_HANDS_TOKEN}'   # 一重の引用符。呼ぶときに展開される
codex mcp add kei-agent-hands --url http://127.0.0.1:8785/mcp --bearer-token-env-var KEI_AGENT_HANDS_TOKEN
```

- 道具と返す項目は [architecture.md](../docs/architecture.md#mcp-サーバー)。開いているかは `curl -s http://127.0.0.1:8785/health`

Dot から使うときは、OpenAI の Secure MCP Tunnel を通す（この Mac から OpenAI へ出ていくだけで、ローカルの待受ポートは外に開かない）。

1. `brew install openai/tools/tunnel-client`
2. <https://platform.openai.com/settings/organization/tunnels> でトンネルを作り、番号を `config.toml` の `[hands]` に `tunnel = "tunnel_..."` と書く。ChatGPT workspaces には自分のものを選ぶ
3. 同じ Platform で鍵（Restricted。Tunnels の Read と Use だけ）を作り、トンネルだけのファイル `kei-agent-tunnel.zsh` に `export CONTROL_PLANE_API_KEY="sk-..."` と書く（共通の `kei-agent.zsh` には書かない。doctor が見る）
4. `deploy/install.sh tunnel`（`deploy/run-tunnel.sh` を launchd に載せる。ログは `tunnel-launchd.log`、つながっているかは `curl -s http://127.0.0.1:8784/readyz`）
5. <https://chatgpt.com/plugins> の「＋」→「MCP アプリを作成」。接続は Tunnel でこのトンネルを選び、認証はなし（合言葉はトンネルが付ける）
6. チャットでは `@Kei Agent` と指名して頼む（指名しないと、ChatGPT は道具を呼ばずにほかの手段で答えることがある）

## 7. 日々の運用

| したいこと | コマンド |
|---|---|
| 点検（読むだけ。`--all` で全部） | `uv run kei-agent doctor` |
| モジュールの一覧・足す・外す | `uv run kei-agent module list` / `add <名前>` / `remove <名前>`（`--dry-run` で見るだけ） |
| モジュールを作る・試す | `uv run kei-agent module new <名前>` / `test <名前>`（[docs/modules.md](../docs/modules.md)） |
| 定期処理を今すぐ1回 | `uv run kei-agent-schedule <名前>`（`--record` を付けなければ記録に残らない） |
| ローカルから依頼を置く | `uv run kei-agent-ask --theme <テーマ> "〜して"`（`--note` で記録だけ） |
| モデルごとの回数・時間・費用を見る | 下のコマンド（読むだけ） |

```zsh
sqlite3 -header -column "file:$HOME/.local/state/kei-agent/kei-agent.db?mode=ro" \
  "SELECT actor, use_case, model, effort, COUNT(*) AS 回数, ROUND(AVG(ended_at - started_at)) AS 平均秒,
          ROUND(SUM(cost_usd), 2) AS 合計USD, SUM(is_error) AS 失敗
   FROM runs WHERE model IS NOT NULL AND started_at > strftime('%s', 'now') - 30 * 86400
   GROUP BY actor, use_case, model, effort ORDER BY 回数 DESC"
```

- `module add` / `remove` は `agents.csv` の `enabled` だけを書き換え、前のものを `agents.csv.bak` に残す（表が無ければ、組み込み全部がオンの表から作る）
- 22:00 の保守: 研究のセッションの記録を90日で消し、使い終わった worktree を消し、`~/research` と `~/kei-agent` を非公開リポジトリに push する（50MB を超えるファイルは外す）
- 別の Mac に移す: `research-data` を `~/research` に clone し、`sqlite3 ~/.local/state/kei-agent/kei-agent.db < ~/kei-agent/state/kei-agent.sql`

## 8. 困ったとき

| 症状 | 見るところ |
|---|---|
| 起動しない | `launchctl print gui/$(id -u)/com.kei-agent.assistant`、`launchd.log`。秘密情報のファイルが無いと止まる |
| Slack に通知が来ない | Dot の Slack 接続・毎時の通知予定・MCP 接続を確認する（[Dot 側の設定](../docs/dots.md#dot-側の設定)） |
| 提出状態が変わらない | Moodle API の URL・本人のトークン・サービスで利用できる関数を確認し、MCP の sync_submissions の enabled と errors を見る |
| 返事が来ない | `agents.csv` の `engine` で AI が選ばれているか。`kei-agent.log` |
| 担当だけ失敗する | `curl -s http://127.0.0.1:<ポート>/.well-known/agent-card.json`、`<名前>-launchd.log`、`KEI_AGENT_A2A_TOKEN` が全部で同じか |
| `can't open input file` | plist が古い起動スクリプトを指している。`deploy/install.sh <名前>` で登録し直す |
| Notion がつながらない | `curl -s http://127.0.0.1:8791/health`、`notion-launchd.log`、`KEI_AGENT_NOTION_GATEWAY_TOKEN` が全部で同じか |
| Notion で `can't reach` | ホームの外を触ろうとしている。ホームがコネクトに共有されているか、`agents.csv` の `notion` 列が合っているか |
| 大学・仕事の連携が見えない | Claude は `agents.csv` の `claude_account` のフォルダとそのログイン、Codex は `codex_account`（無ければ `~/.codex`）の Codex アプリの連携 |
| 声が出ない・聞かない | `ffmpeg`、マイクの許可、MCP の `voice` の設定、`voice-launchd.log` |
| 上限に当たった | MCP の失敗結果と通知を確認する。再開時刻が分かれば、時刻を過ぎてから同じ conversation で続ける |
| 自己改善のあと起動しない | 3回失敗すると `deploy/run.sh` が `git revert` して前の版で起動し、Slack で知らせる |
