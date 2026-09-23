#!/bin/zsh
# Kei Agent を launchd に登録する（ログイン時に起動し、落ちたら再起動する）。
# 使い方: deploy/install.sh               登録して起動
#         deploy/install.sh remove        登録を外す
#         deploy/install.sh course        大学エージェント（A2A サーバー）を登録
#         deploy/install.sh course remove 大学エージェントの登録を外す
#         deploy/install.sh research      研究エージェント（A2A サーバー）を登録
#         deploy/install.sh research remove 研究エージェントの登録を外す
#         deploy/install.sh work          仕事エージェント（A2A サーバー）を登録
#         deploy/install.sh voice         声のレイヤ（A2A サーバー＋マイク）を登録
#         deploy/install.sh notion-gateway 研究 Claude 用の Notion ゲートウェイを登録
set -eu

# 引数に course / research などを付けると、そのプロセスのほうを登録する
case "${1:-}" in
  course|research|work|voice|notion-gateway)
    LABEL="com.kei-agent.${1}"
    shift
    ;;
  *)
    LABEL="com.kei-agent.assistant"
    ;;
esac
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
# bootout の完了は非同期になることがあり、直後の bootstrap は
# "Input/output error (5)" で競合する。短く再試行してから失敗を返す。
bootstrapped=false
for _ in {1..5}; do
  if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
    bootstrapped=true
    break
  fi
  sleep 1
done
if [[ "$bootstrapped" != true ]]; then
  echo "LaunchAgent の登録に失敗しました: $LABEL" >&2
  exit 1
fi
launchctl kickstart -k "$DOMAIN/$LABEL"
echo "登録しました（$LABEL）。ログ: $LOG_DIR/"
echo "状態の確認: launchctl print $DOMAIN/$LABEL | grep -E 'state|last exit'"
