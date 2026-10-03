"""本体と手動の定期処理で共用する実行サービス。通知は MCP 経由で Dot に渡す。"""

from kei_agent.conversation.assistant import Assistant
from kei_agent.conversation.outbox import Outbox
from kei_agent.execution.jobs import JobManager
from kei_agent.storage.notion_hub import load_hub
from kei_agent.storage.notion_store import load_notion


def create_assistant(config, store, pueue) -> Assistant:
    return Assistant(config=config, store=store, slack=Outbox(config, store),
                     jobs=JobManager(config, store, pueue),
                     notion=load_notion(config), hub=load_hub(config))
