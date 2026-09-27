"""A2A サーバーの土台。研究のエージェントと、モジュールの担当プロセスで共通の部分だけを置く。

エージェントごとの中身（名刺に載せる仕事と、そのこなし方）は `kei_agent_research` と、
モジュールの `modules/<名前>/agent.py`（大学・仕事・知識など）にある。ここは待ち受け（`server.py`）、名刺の形
（`card.py`）、仕事の受け付け（`executor.py`）、provider の動かし方（`run.py`）、返事の封筒（`envelope.py`）、
モジュールの担当プロセスの窓口（`api.py`）と共通の起動コマンド（`launch.py`）。
"""
