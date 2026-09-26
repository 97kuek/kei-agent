#!/bin/zsh
# launchd から Notion ゲートウェイ（Notion に届く唯一の口。MCP と /notion/v1）を起動する。
# 127.0.0.1:8791 でだけ待ち受ける。NOTION_TOKEN を持つのは、このプロセスだけ。
set -eu

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# 読むのは共通の秘密情報だけ（NOTION_TOKEN と、client ごとの合言葉の元になる親の合言葉）。
# ほかのエージェントの秘密情報は読まない
SECRETS="$HOME/.config/zsh/local/kei-agent.zsh"
if [[ ! -r "$SECRETS" ]]; then
  echo "秘密情報のファイルがありません: $SECRETS（deploy/README.md を参照）" >&2
  exit 1
fi
source "$SECRETS"

REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"

# ログは launchd の標準出力（~/Library/Logs/kei-agent/notion-gateway-launchd.log）に出る
trim_launchd_log notion-gateway-launchd.log

cd "$REPO"
exec uv run --frozen kei-agent-notion-gateway
