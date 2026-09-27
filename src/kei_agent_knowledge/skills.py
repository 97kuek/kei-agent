"""知識エージェントの仕事の名前。オーケストレーターはこの id を指定して頼む。

名刺（`card.py`）は a2a-sdk に依存するので、名前だけをここに置く。本体側（`modules/knowledge/module.py`）にも
同じ名前がある（モジュールが読み込めるのは kei_agent.api だけなので）。
"""

READING_DIGEST = "reading-digest"
PAPER_DIGEST = "paper-digest"
# 定型に当てはまらない質問の窓口（どのエージェントでも同じ名前。docs/architecture.md の「振り分けと A2A」）
ASK = "ask"
