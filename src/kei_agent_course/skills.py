"""大学エージェントの仕事の名前。オーケストレーターはこの id を指定して頼む。

名刺（`card.py`）は a2a-sdk に依存するので、本体（`kei_agent.course`）からも読めるよう、名前だけをここに置く。
"""

SYNC_ASSIGNMENTS = "sync-assignments"
LIST_DUE = "list-due"
LIST_CALENDAR_ASSIGNMENTS = "list-calendar-assignments"
LIST_CLASSES = "list-classes"
LIST_CURRENT_COURSES = "list-current-courses"
TIME_REPORT = "time-report"
# 定型に当てはまらない質問の窓口（どのエージェントでも同じ名前。docs/architecture.md の「振り分けと A2A」）
ASK = "ask"
