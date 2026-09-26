"""デプロイのあと、全部のプロセスが新しい版で動いているかの確かめ（kei_agent.deploy_check）。"""

from kei_agent import deploy_check


def test_every_process_is_checked(config):
    config.a2a.agents.update({"course": "http://127.0.0.1:8787", "knowledge": "http://127.0.0.1:8792/"})
    targets = deploy_check.targets(config)
    assert targets["course"] == "http://127.0.0.1:8787/.well-known/agent-card.json"
    assert targets["knowledge"] == "http://127.0.0.1:8792/.well-known/agent-card.json"
    assert targets["notion-gateway"] == "http://127.0.0.1:8791/health"


async def test_stale_waits_for_restarts_and_names_the_ones_left(config, monkeypatch):
    """起動し直すのを待ちながら見直す。待っても古い（答えない）ものだけを返す。"""
    config.a2a.agents.clear()
    config.a2a.agents.update({"course": "http://c", "work": "http://w"})
    answers = {"http://c/.well-known/agent-card.json": ["old", "new"],
               "http://w/.well-known/agent-card.json": ["", ""],
               "http://127.0.0.1:8791/health": ["new", "new"]}

    async def running_version(http, url):
        # 見直すたびに次の答え（尽きたら最後の答えのまま）
        queue = answers.get(url, ["new"])
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(deploy_check, "running_version", running_version)
    monkeypatch.setattr(deploy_check, "POLL_SECONDS", 0)
    left = await deploy_check.stale(config, "new", wait=0.2)
    assert "course" not in left and left["work"] == ""
