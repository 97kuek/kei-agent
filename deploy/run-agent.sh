#!/bin/zsh
# launchd から担当（A2A サーバー）を起動する。127.0.0.1 でだけ待ち受ける。
# 使い方: deploy/run-agent.sh <research|course|work|knowledge|voice>。どれも同じ形で、違うのは名前だけ。
# 声のレイヤ（voice）は、口（A2A）と耳（マイク）を同じプロセスで持つ。マイクは既定では開けない（App Home から入れる）
set -eu

AGENT="${1:-}"
case "$AGENT" in
  research|course|work|knowledge|voice) ;;
  *)
    echo "知らない担当です: ${AGENT:-（名前なし）}（research / course / work / knowledge / voice のどれか）" >&2
    exit 1
    ;;
esac

REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"

require_secrets
source "$SECRETS"
AGENT_SECRETS="$SECRETS_DIR/kei-agent-$AGENT.zsh"
[[ -r "$AGENT_SECRETS" ]] && source "$AGENT_SECRETS"
drop_notion_secrets

# ログは launchd の標準出力（~/Library/Logs/kei-agent/<名前>-launchd.log）に出る
trim_launchd_log "$AGENT-launchd.log"
launch "$AGENT" "kei-agent-$AGENT"
