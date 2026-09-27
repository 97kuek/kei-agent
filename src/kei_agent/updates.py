"""新しい版で起動し直す（入れ替え）。本体の常駐の仕組み（deploy/run.sh）といっしょに動く。

取り込んだあとは、動いている AI の作業が終わるのを待ってから、ほかのプロセスを起動し直し、本体を終える
（launchd が新しい版で起動する）。その前に update-pending を残す。新しい版が Slack につながったら本体が消し、
消えないまま起動を繰り返したら、deploy/run.sh が前の版に戻して update-rolled-back を残す。
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kei_agent import modules
from kei_agent.config import Config

log = logging.getLogger(__name__)

# 入れ替える前に残すファイル。新しい版が Slack につながったら消す
PENDING_NAME = "update-pending"
# deploy/run.sh が、起動できずに戻したときに残すファイル
ROLLED_BACK_NAME = "update-rolled-back"


@dataclass(frozen=True)
class Update:
    """入れ替えたあとの起動で分かる、その結果。state は done（新しい版で動いた）か rolled_back（前の版に戻した）。"""
    state: str
    # 取り込む前のコミット
    previous: str
    # 入れ替えを頼んだときに添えた1行（どのスレッドの取り込みか、など）
    note: str


def pending_path(config: Config) -> Path:
    return config.state_dir / PENDING_NAME


def rolled_back_path(config: Config) -> Path:
    return config.state_dir / ROLLED_BACK_NAME


def mark_pending(config: Config, previous: str, note: str = "") -> None:
    """入れ替える前に残す（1行目が前のコミット、2行目が起動を試した回数、3行目が添えた1行）。"""
    note = " ".join(note.split())
    pending_path(config).write_text(f"{previous}\n0\n{note}\n", encoding="utf-8")


def _read(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    return (lines[0] if lines else "", lines[2] if len(lines) > 2 else "")


def take_update(config: Config) -> Update | None:
    """新しい版が Slack につながったあとに呼ぶ。入れ替えたあとの起動なら、その結果を返して印を消す。"""
    rolled = _read(rolled_back_path(config))
    if rolled is not None:
        rolled_back_path(config).unlink(missing_ok=True)
        # 戻したあとに残っていた印も片づける（run.sh は戻すときに消している）
        pending_path(config).unlink(missing_ok=True)
        return Update("rolled_back", *rolled)
    pending = _read(pending_path(config))
    if pending is not None:
        pending_path(config).unlink(missing_ok=True)
        return Update("done", *pending)
    return None


def restart_service(name: str) -> bool:
    """launchd の com.kei-agent.<name> を、新しい版で起動し直す。"""
    label = f"com.kei-agent.{name}"
    proc = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        log.warning("%s を起動し直せません: %s", label, (proc.stderr or "").strip()[:200])
    return proc.returncode == 0


def installed_services(home: Path | None = None) -> list[str]:
    """launchd に登録した Kei Agent のプロセス（本体を除く）。A2A ではない常駐のプロセス（Notion のゲートウェイ）を
    先にする（ほかのプロセスが使う。deploy/restart-all.sh と同じ）。"""
    agents_dir = (home or Path.home()) / "Library" / "LaunchAgents"
    names = [path.name.removeprefix("com.kei-agent.").removesuffix(".plist")
             for path in sorted(agents_dir.glob("com.kei-agent.*.plist"))]
    services = {name for name, spec in modules.known().items() if spec.service}
    return sorted((name for name in names if name != "assistant"), key=lambda name: name not in services)


def restart_agents() -> list[str]:
    """本体のほかのプロセス（ゲートウェイ・担当・声）を、新しい版で起動し直す。

    本体は launchd が入れ替えるが、ほかは動き続けてしまう（古いコードのまま）。本体が静かになってから呼ぶ。
    """
    done = [name for name in installed_services() if restart_service(name)]
    if done:
        log.info("起動し直しました: %s", "、".join(done))
    return done
