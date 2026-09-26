"""知識エージェントの仕事の名前。オーケストレーターはこの id を指定して頼む。

名刺（`card.py`）は a2a-sdk に依存するので、本体（`kei_agent.knowledge`）からも読めるよう、名前だけをここに置く。
"""

READING_DIGEST = "reading-digest"
PAPER_DIGEST = "paper-digest"
# 定型に当てはまらない質問の窓口（どのエージェントでも同じ名前。docs/architecture.md の「振り分けと A2A」）
ASK = "ask"
