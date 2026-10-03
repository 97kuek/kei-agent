"""通知と実行状態は MCP の実行結果に合わせ、読み取りだけでは通知を消さない。"""

import pytest
from fakes import FakeAI, final_answer, make_assistant

from kei_agent.conversation.hands import Hands
from kei_agent.conversation.outbox import Outbox
from kei_agent.execution import runner


@pytest.mark.parametrize(('answer', 'state', 'event'), [
    ('終わりました', 'done', 'done'),
    ('❓ 確認: 続けますか', 'needs_input', 'awaiting'),
])
async def test_mcp_emits_start_and_the_actual_result(config, store, monkeypatch, answer, state, event):
    assistant, _ = make_assistant(config, store)
    events = []
    monkeypatch.setattr(assistant, 'emit', lambda kind, **data: events.append((kind, data)))
    (config.research_root / 'vlm').mkdir(parents=True)
    ai = FakeAI()
    ai.answer(final_answer(answer))
    monkeypatch.setattr(runner, 'run_model', ai)

    result = await Hands(assistant).run('vlm', '調べて', conversation='events')

    assert result['status'] == state
    assert [kind for kind, _ in events] == ['working', event]
    assert all(data['theme'] == 'vlm' for _, data in events)


async def test_reading_notices_without_arguments_does_not_acknowledge_them(config, store):
    assistant, _ = make_assistant(config, store)
    assistant.slack = Outbox(config, store)
    await assistant.slack.chat_postMessage(channel='vlm', text='ジョブが終わりました')
    hands = Hands(assistant)

    first = hands.notices()
    assert hands.notices()['notices'] == first['notices']
    assert first['notices']
    ids = [item['id'] for item in first['notices']]
    assert hands.notices(done=ids)['notices'] == []
