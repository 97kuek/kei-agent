# Ezra のセットアップ

`docs/plan.md` 第6章のステップ1（準備）とステップ10（常時起動）の手順。

## 1. Slack のワークスペースとチャンネル

1. 個人用のワークスペースを作る
2. チャンネルを作る。名前は `config.toml` の `[channels]` と合わせる

   | 種類 | チャンネル名（既定） | Ezra の動き |
   |---|---|---|
   | 研究全体 | `#research-overview` | すべてのテーマを読むだけ。書き込みは `~/research/_overview/` |
   | 中長期の方針 | `#research-strategy` | 同上 |
   | Assistantの改善 | `#assistant-improve` | 要望を `docs/backlog.md` に記録するだけ |
   | 研究テーマ | 上記以外（例: `#vlm-counting`） | `~/research/<チャンネル名>/` で作業する |

## 2. Slack App「Ezra」

1. <https://api.slack.com/apps> → **Create New App** → **From a manifest** → ワークスペースを選び、`slack/manifest.yaml` の中身を貼る
2. **Install App** → ワークスペースにインストールし、**Bot User OAuth Token**（`xoxb-`）を控える
3. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**。scope に `connections:write` を付け、トークン（`xapp-`）を控える
4. 自分のSlackユーザーIDを控える（Slack でプロフィール → ︙ → **メンバーIDをコピー**。`U` で始まる）

## 3. 秘密情報

`~/.config/zsh/local/research-assistant.zsh` を作る（Git に入れない）。

```zsh
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export EZRA_ALLOWED_USER_ID="U..."
# 任意: Semantic Scholar の APIキー（なくても動くが、混雑時に 429 になりやすい）
# export S2_API_KEY="..."
```

`chmod 600` にしておく。`~/.zshrc` から `~/.config/zsh/local/*.zsh` を読んでいれば、ターミナルでもそのまま使える。

## 4. 手元で起動して確かめる

```zsh
brew install pueue && brew services start pueue   # 済んでいれば不要
uv sync
uv run ezra
```

確認すること（ステップ1〜3）:

- ログに `Ezra を起動しました` が出て、Slack で Ezra がオンラインになる
- テーマのチャンネルに Ezra を招待すると、`~/research/<チャンネル名>/` と `CLAUDE.md` ができる
- `@Ezra このディレクトリの中身を教えて` にスレッドで返信が来る。メンションなしのメッセージには反応しない

## 5. 常時起動（launchd）

launchd から起動したプロセスは、macOS の保護フォルダ（`~/Documents`、`~/Desktop`、`~/Downloads`）を読めない。
リポジトリをその外（例: `~/src/research-support-assistant`）に置いてから登録する。

```zsh
deploy/install.sh          # 登録して起動（ログイン時に起動し、落ちたら再起動する）
deploy/install.sh remove   # 登録を外す
tail -f ~/Library/Logs/ezra/ezra.log
launchctl print gui/$(id -u)/com.ezra.assistant | grep -E 'state|last exit'
```

`claude` は、ふだんのログイン（キーチェーン）で動く。launchd からログイン情報を読めないときは、
`claude setup-token` で作ったトークンを `CLAUDE_CODE_OAUTH_TOKEN` として秘密情報のファイルに足す。

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
source ~/.config/zsh/local/research-assistant.zsh
uv run ezra-schedule daily        # night / literature / daily / review
```

テーマの先行研究を見張るには、テーマの `CLAUDE.md` の「## 検索キーワード」に、1行に1つ英語で書く。

## 状態の置き場所

| もの | 場所 |
|---|---|
| 設定 | `config.toml`（`EZRA_CONFIG` で別のファイルを指定できる） |
| スレッドとセッション、ジョブ、実行時間 | `~/.local/state/ezra/ezra.db` |
| テーマの作業用ディレクトリ | `~/research/` |
| ジョブ | `pueue status --group ezra` |
| ログ | `~/Library/Logs/ezra/ezra.log`（launchd のとき） |
