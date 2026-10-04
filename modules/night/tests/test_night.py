"""夜間モジュールを本番と同じ窓口で動かす。"""

from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]


async def test_without_notion(module_kit):
    kit = module_kit(MODULE)
    kit.assistant.notion = None
    assert await kit.schedule("night") == {"status": "no_notion"}


async def test_read_failure_is_recorded(module_kit):
    kit = module_kit(MODULE)
    kit.notion.fail = True
    detail = await kit.schedule("night")
    assert detail["status"] == "error"
    assert kit.ai.calls == []


async def test_task_without_theme_waits_for_confirmation(module_kit):
    kit = module_kit(MODULE)
    task = kit.notion.add_task("テーマを選ぶ")
    detail = await kit.schedule("night")
    assert detail["tasks"][0]["status"] == task.status == "Waiting"
    assert kit.ai.calls == []
