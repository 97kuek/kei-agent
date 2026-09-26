#!/bin/zsh
# launchd から Notion ゲートウェイ（Notion に届く唯一の口。MCP と /notion/v1）を起動する。
# 127.0.0.1:8791 でだけ待ち受ける。NOTION_TOKEN を持つのは、このプロセスだけ。
set -eu

REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"

# 読むのは共通の秘密情報だけ（NOTION_TOKEN と、client ごとの合言葉の元になる親の合言葉）。
# ほかの担当の秘密情報は読まない
require_secrets
source "$SECRETS"

# ログは launchd の標準出力（~/Library/Logs/kei-agent/notion-gateway-launchd.log）に出る
trim_launchd_log notion-gateway-launchd.log
launch "" kei-agent-notion-gateway
