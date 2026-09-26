"""研究エージェントの仕事の名前。オーケストレーターはこの id を指定して頼む。

名刺（`card.py`）は a2a-sdk に依存するので、本体（`kei_agent.research`）からも読めるよう、名前だけをここに置く。
長い処理（ジョブ）の pueue を持つのは研究エージェントで、どのスレッドのジョブかの管理は本体。
"""

SUBMIT_JOB = "submit-job"
LIST_JOBS = "list-jobs"
CANCEL_JOB = "cancel-job"
FORGET_JOB = "forget-job"
# 定型に当てはまらない質問の窓口（どのエージェントでも同じ名前。docs/architecture.md の「振り分けと A2A」）
ASK = "ask"
