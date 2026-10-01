#!/bin/zsh
# launchd から担当（A2A サーバー）を起動する。127.0.0.1 でだけ待ち受ける。
# 使い方: deploy/run-agent.sh <名前>。名前は常駐のプロセスを持つモジュール（module.toml に [process] がある）。
# どれも同じ形で、違うのは名前だけ。共通の起動コマンド（kei-agent-module <名前>）が modules/<名前>/agent.py
# （A2A ではない常駐のプロセスなら service.py）を動かす。研究（research）もモジュール。
# 声（voice）は、口（A2A）と耳（マイク）を同じプロセスで持つ。マイクは既定では開けない（App Home から入れる）。
# Notion（notion）は A2A ではない常駐のプロセス（ゲートウェイ）で、Notion の鍵を持てるのはこれだけ
set -eu

AGENT="${1:-}"
REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"
agents=($(agent_names))
if [[ -z "$AGENT" || ${agents[(Ie)$AGENT]} -eq 0 ]]; then
  echo "知らない担当です: ${AGENT:-（名前なし）}（${(j: / :)agents} のどれか）" >&2
  exit 1
fi

require_secrets
source "$SECRETS"
AGENT_SECRETS="$SECRETS_DIR/kei-agent-$AGENT.zsh"
[[ -r "$AGENT_SECRETS" ]] && source "$AGENT_SECRETS"
# Notion の鍵（NOTION_TOKEN）を残すのは、Notion のモジュールのプロセス（ゲートウェイ）だけ
if [[ "$AGENT" != "$NOTION_MODULE" ]]; then
  drop_notion_secrets
fi

# ログは launchd の標準出力（~/Library/Logs/kei-agent/<名前>-launchd.log）に出る
trim_launchd_log "$AGENT-launchd.log"
# 依存は担当に共通のグループ（モジュールを足しても pyproject.toml を直さない）
launch agents kei-agent-module "$AGENT"
