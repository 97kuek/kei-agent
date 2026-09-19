#!/bin/zsh
# launchd から Ezra を起動する。launchd は ~/.zshrc を読まないので、ここで PATH と秘密情報を用意する。
set -eu

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SECRETS="$HOME/.config/zsh/local/research-assistant.zsh"
if [[ ! -r "$SECRETS" ]]; then
  echo "秘密情報のファイルがありません: $SECRETS（deploy/README.md を参照）" >&2
  exit 1
fi
source "$SECRETS"

# ログは Ezra 自身が 5MB ごとに回す。launchd の標準出力には、起動に失敗したときの出力だけが残る
export EZRA_LOG_FILE="$HOME/Library/Logs/ezra/ezra.log"

# launchd の出力は回らないので、起動のたびに大きすぎるものを捨てる
# （設定を間違えると KeepAlive で 30 秒ごとに再起動し、同じエラーが積もり続ける）
LAUNCHD_LOG="$HOME/Library/Logs/ezra/launchd.log"
if [[ -f "$LAUNCHD_LOG" ]] && (( $(stat -f%z "$LAUNCHD_LOG") > 5242880 )); then
  : > "$LAUNCHD_LOG"
fi

REPO="${0:A:h:h}"
cd "$REPO"

# Ezra が自分を入れ替えたあとの起動（src/ezra/improve.py）。
# 新しい版が Slack につながれば update-pending は消える。消えないまま起動を繰り返したら、前の版に戻す。
STATE="${EZRA_STATE_DIR:-$HOME/.local/state/ezra}"
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

exec uv run --frozen ezra
