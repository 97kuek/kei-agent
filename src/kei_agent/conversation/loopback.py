"""本体のプロセスで、127.0.0.1 だけに HTTP サーバーを開く。A2A（questions.py）と MCP（hands_server.py）。

開けなくても実行サービスは止めず、Outbox に問題を記録する。
"""

from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Callable

log = logging.getLogger(__name__)
LOOPBACK = frozenset({"127.0.0.1", "localhost"})


def loopback_host(url: str) -> tuple[str, int] | None:
    """url が 127.0.0.1（か localhost）のポートなら (host, port)。違えば None。"""
    parsed = urllib.parse.urlparse(url)
    return (parsed.hostname, parsed.port) if parsed.hostname in LOOPBACK and parsed.port else None


async def serve_loopback(assistant, url: str, label: str, app_for: Callable[[str], object]) -> None:
    """url の口を開いて、止められるまで待つ。app_for は host から ASGI のアプリを作る。label は知らせに使う名前。"""
    import uvicorn

    where = loopback_host(url)
    if where is None:
        await assistant.notify_trouble(f"{label}は 127.0.0.1 のポートで指定してください: {url}")
        return
    host, port = where
    server = uvicorn.Server(uvicorn.Config(app_for(host), host=host, port=port, log_level="warning"))
    log.info("%sを開きます（%s）", label, url)
    try:
        await server.serve()
    except SystemExit:
        # ポートが使われているなど。uvicorn は SystemExit で知らせる
        log.error("%sを開けませんでした（%s）", label, url)
        await assistant.notify_trouble(f"{label}（{url}）を開けませんでした")
    except Exception:
        log.exception("%sが止まりました（%s）", label, url)
        await assistant.notify_trouble(f"{label}（{url}）が止まりました。Kei Agent のログを見てください")
