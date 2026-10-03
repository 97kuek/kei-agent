"""大学の担当の仕事の名前。本体側（module.py）はこの id を指定して頼み、担当側（agent.py）は名刺に載せる。

どちらからも読むので、ここは何も読み込まない（本体側に a2a-sdk を持ち込まない）。
"""

SYNC_ASSIGNMENTS = "sync-assignments"
SYNC_SUBMISSIONS = "sync-submissions"
LIST_DUE = "list-due"
LIST_CALENDAR_ASSIGNMENTS = "list-calendar-assignments"
LIST_CLASSES = "list-classes"
LIST_CURRENT_COURSES = "list-current-courses"
TIME_REPORT = "time-report"
