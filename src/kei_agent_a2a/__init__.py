"""A2A サーバーの土台。大学・研究・仕事のエージェントで共通の部分だけを置く。

エージェントごとの中身（名刺に載せる仕事と、そのこなし方）は `kei_agent_course`・`kei_agent_research`・
`kei_agent_work` にある。ここは待ち受け（`server.py`）、名刺の形（`card.py`）、仕事の受け付け
（`executor.py`）、provider の動かし方（`claude.py`）、返事の封筒（`envelope.py`）。
"""
