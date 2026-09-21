"""机の上のロボットの顔（docs/voice.md の5節）。

**声はもうここを通らない。** Realtime API が音をそのまま返すので、合成した wav を投げる口
（`POST /play_wav`）は使わない。残っているのは**顔と首**だけ。

`stackchan-atama` の口のうち、使うもの:
- `GET /status`                   … 生きているか
- `GET /face?expression=<6種>`    … 表情

まだ買っていないので、住所（`KEI_AGENT_STACKCHAN_URL`）を書かないあいだは何もしない。
声は Mac のスピーカーから出る。
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

URL_ENV = "KEI_AGENT_STACKCHAN_URL"
# 生きているかを見に行くときの待ち時間。机の上の LAN なので短くてよい
PROBE_SECONDS = 1.5


class Face:
    """表情を変えるところ。住所が無ければ、呼ばれても何もしない。"""

    def __init__(self, url: str = "", env: dict | None = None):
        env = os.environ if env is None else env
        self.url = (url or env.get(URL_ENV, "")).rstrip("/")

    @property
    def present(self) -> bool:
        """机の上にいるか。住所を書いていなければ、いつも False。"""
        if not self.url:
            return False
        return self._get("/status")

    def show(self, expression: str) -> bool:
        """表情を変える。いなければ何もしない（`False` を返すだけ）。"""
        if not self.url:
            return False
        return self._get(f"/face?expression={expression}")

    def _get(self, path: str) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}{path}", timeout=PROBE_SECONDS):
                return True
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.info("机の上のロボットに届きません（%s）: %s", path, e)
            return False
