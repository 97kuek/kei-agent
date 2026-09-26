# launchd から起動する run*.sh が読む共通の処理（直接は動かさない）。

# launchd の出力は回らないので、起動のたびに大きすぎるものを捨てる
# （設定を間違えると KeepAlive で 30 秒ごとに再起動し、同じエラーが積もり続ける）
LAUNCHD_LOG_LIMIT=5242880
trim_launchd_log() {
  local log="$HOME/Library/Logs/kei-agent/$1"
  if [[ -f "$log" ]] && (( $(stat -f%z "$log") > LAUNCHD_LOG_LIMIT )); then
    : > "$log"
  fi
}

# Notion の鍵を持つのは Notion ゲートウェイ（run-notion-gateway.sh）だけ。ほかのプロセスは、
# 共通の秘密情報を読んだあとで消す（Notion には client ごとの合言葉でゲートウェイを通して届く）
drop_notion_secrets() {
  unset NOTION_TOKEN NOTION_COURSE_TOKEN
}
