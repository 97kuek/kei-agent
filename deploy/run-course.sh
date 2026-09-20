#!/bin/zsh
# launchd から大学エージェント（A2A サーバー）を起動する。127.0.0.1 でだけ待ち受ける。
set -eu

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SECRETS="$HOME/.config/zsh/local/research-assistant.zsh"
if [[ ! -r "$SECRETS" ]]; then
  echo "秘密情報のファイルがありません: $SECRETS（deploy/README.md を参照）" >&2
  exit 1
fi
source "$SECRETS"

export KEI_AGENT_LOG_FILE="$HOME/Library/Logs/kei-agent/course.log"

REPO="${0:A:h:h}"
cd "$REPO"
exec uv run --frozen --group course kei-agent-course
