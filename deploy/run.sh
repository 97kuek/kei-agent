#!/bin/zsh
# launchd から Kei Agent を起動する。launchd は ~/.zshrc を読まないので、ここで PATH と秘密情報を用意する。
set -eu

REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"

require_secrets
source "$SECRETS"
drop_notion_secrets

# ログは Kei Agent 自身が 5MB ごとに回す。launchd の標準出力には、起動に失敗したときの出力だけが残る
export KEI_AGENT_LOG_FILE="$HOME/Library/Logs/kei-agent/kei-agent.log"
trim_launchd_log launchd.log
cd "$REPO"

# Kei Agent が自分を入れ替えたあとの起動（src/kei_agent/improve.py）。
# 新しい版が Slack につながれば update-pending は消える。消えないまま起動を繰り返したら、前の版に戻す。
STATE="${KEI_AGENT_STATE_DIR:-$HOME/.local/state/kei-agent}"
PENDING="$STATE/update-pending"
MAX_ATTEMPTS=3
if [[ -f "$PENDING" ]]; then
  PREV=$(sed -n 1p "$PENDING")
  ATTEMPTS=$(( $(sed -n 2p "$PENDING") + 1 ))
  THREAD=$(sed -n 3p "$PENDING")
  if (( ATTEMPTS > MAX_ATTEMPTS )); then
    echo "新しい版で $MAX_ATTEMPTS 回起動できませんでした。$PREV に戻します" >&2
    git revert --no-edit "$PREV..HEAD" >&2 || git reset --hard "$PREV" >&2
    printf '%s\n%s\n%s\n' "$PREV" "$ATTEMPTS" "$THREAD" > "$STATE/update-rolled-back"
    rm -f "$PENDING"
  else
    printf '%s\n%s\n%s\n' "$PREV" "$ATTEMPTS" "$THREAD" > "$PENDING"
  fi
fi

# 声のレイヤからの問い合わせ口（A2A のサーバー）を開くので、担当と同じグループも入れる
launch agents kei-agent
