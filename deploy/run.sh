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

cd "${0:A:h}/.."
exec uv run --frozen ezra
