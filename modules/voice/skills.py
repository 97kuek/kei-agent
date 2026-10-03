"""声のモジュールの名前（本体側の module.py と、担当プロセスの agent.py の両方が読む）。"""

# 担当の仕事の名前。本体側はこの id を指定して知らせる
NOTIFY = "notify"
# MCP の通知設定のオン・オフを残す記録（core.records / executor.records の種類と鍵）
SWITCH = "switch"
NOTIFY_KEY = "notify"
LISTEN_KEY = "listen"
