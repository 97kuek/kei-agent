#!/bin/zsh
# Kei Agent を launchd に登録する（ログイン時に起動し、落ちたら再起動する）。
# 使い方: deploy/install.sh        登録して起動
#         deploy/install.sh remove 登録を外す
set -eu

LABEL="com.kei-agent.assistant"
REPO="${0:A:h:h}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/kei-agent"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "remove" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "登録を外しました"
  exit 0
fi

# launchd から起動したプロセスは、macOS の保護フォルダ（書類・デスクトップ・ダウンロード）を読めない
case "$REPO" in
  "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
    echo "リポジトリが macOS の保護フォルダの中にあります: $REPO" >&2
    echo "launchd から起動すると読めないため、~/src などに移してから実行してください（deploy/README.md を参照）" >&2
    exit 1
    ;;
esac

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__LOG_DIR__|$LOG_DIR|g" "$REPO/deploy/$LABEL.plist.template" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"
echo "登録しました。ログ: $LOG_DIR/kei-agent.log"
echo "状態の確認: launchctl print $DOMAIN/$LABEL | grep -E 'state|last exit'"
