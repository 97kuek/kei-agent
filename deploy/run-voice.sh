#!/bin/zsh
# launchd から声のレイヤ（A2A サーバー）を起動する。127.0.0.1 でだけ待ち受ける。
# 口（A2A）と耳（マイク）を同じプロセスで持つ。マイクは既定では開けない（App Home から入れる）。
set -eu

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# 秘密情報は、共通のものと、このエージェントだけのものに分けてある（docs/architecture.md の「振り分けと A2A」）。
# こうすると、ほかのエージェントのトークンがこのプロセスに載らない
SECRETS="$HOME/.config/zsh/local/kei-agent.zsh"
AGENT_SECRETS="$HOME/.config/zsh/local/kei-agent-voice.zsh"
if [[ ! -r "$SECRETS" ]]; then
  echo "秘密情報のファイルがありません: $SECRETS（deploy/README.md を参照）" >&2
  exit 1
fi
source "$SECRETS"
[[ -r "$AGENT_SECRETS" ]] && source "$AGENT_SECRETS"

REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"
drop_notion_secrets

# ログは launchd の標準出力（~/Library/Logs/kei-agent/voice-launchd.log）に出る
trim_launchd_log voice-launchd.log

cd "$REPO"
exec uv run --frozen --group voice kei-agent-voice
