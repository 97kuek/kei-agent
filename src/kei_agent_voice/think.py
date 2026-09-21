"""調べもの。研究・授業・仕事の中身を Codex に読ませて答えてもらう（docs/voice.md の3節）。

声の会話そのものは Realtime API が持つ。ここに回ってくるのは、**ファイルを読まないと
答えられないもの**だけ（`tools.ask_research` から呼ばれる）。Codex にするのは、購読の枠が
Slack の Kei Agent（Claude）と分かれるから。声で長く話した日に、研究の作業が止まらない。

**実測（2026-09-21、`codex exec resume`）**

- プロセスを起こして会話を戻すまで **0.17秒**。残りは全部モデルの往復（短い返事で約5秒）
- 返事は**途中では出てこない**。`item.completed` で丸ごと届く
- 放っておくと長い。6.2 秒で最初の答えを書いたあと**コマンドを3回走らせ、22 秒まで続けた**

開き直しが 0.17 秒なので、**常駐プロセスは持たない**（毎回 `codex exec resume`。落ちても
次の問いで勝手に戻る）。声の会話の途中なので、**25秒で諦める**。

`~/research/` は**読ませるだけ**にする（`sandbox_mode=read-only`）。書いたり動かしたりするのは
Slack の Kei Agent の仕事。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 読ませるだけにする。`-s read-only` と同じことだが、`resume` が `-s` を受け付けないので設定で渡す
SANDBOX = ("-c", 'sandbox_mode="read-only"')
# 声の返事なので、深く考えさせない
EFFORT = ("-c", "model_reasoning_effort=low")
# 会話の区切り。日付が変わるか、これだけ離れたら新しい会話にする
GAP_SECONDS = 2 * 3600
# 待つ上限。声の会話の途中なので、長く黙らせない
VOICE_TIMEOUT = 25.0
LIMIT = re.compile(r"usage limit", re.IGNORECASE)

INSTRUCTIONS = """あなたは Kei Agent の「調べる人」。声で話している相手に、短く答える。

- **話し言葉で、3文以内。** 読み上げられるので、箇条書き・記号・URL・コードは書かない
- `~/research/` を読んで答える。ここに回ってくるのは、読まないと分からないことだけ
- **最初の1回で答えきる。** 読むファイルは必要最小限に。声で待てるのは10秒まで
- 固有名詞は音声認識で崩れて届く（`amr-query` が「アムルクエリー」、`Slack` が「スラック」）。
  フォルダ名から推し量って読み替える
- 分からないことは「分からない」と言う。推測で埋めない
- **書き込みや実行はしない**（作業するのは Slack の Kei Agent）
"""


@dataclass
class Turn:
    """1回のやりとりの結果。"""
    text: str = ""
    failed: str = ""


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


class Codex:
    """Codex CLI を、同じ会話の続きとして呼ぶ。"""

    def __init__(self, cwd: Path, state_dir: Path, command: tuple[str, ...] = ("codex",)):
        self.cwd = cwd
        self.state_dir = state_dir
        self.command = command

    # 会話（日付が変わるか、2時間空いたら新しくする）

    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"

    def session_id(self, now: float) -> str | None:
        """続けてよい会話があれば、その id。無ければ None（新しく始める）。"""
        try:
            saved = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if saved.get("day") != _day(now) or now - float(saved.get("at") or 0) > GAP_SECONDS:
            return None
        return saved.get("session_id") or None

    def remember(self, session_id: str, now: float) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(
            json.dumps({"day": _day(now), "session_id": session_id, "at": now}), encoding="utf-8")

    # やりとり

    def build_command(self, session_id: str | None, out: Path) -> list[str]:
        """`codex exec`（または `resume`）の呼び出し方。

        **`resume` は `-s` と `-C` を受け付けない**（`error: unexpected argument '-s' found` で
        即座に終わる）。読ませるだけにするのは `-c sandbox_mode` で指定し、置き場所は
        プロセスの作業ディレクトリで渡す（続きの会話は、始めた場所を覚えている）。
        """
        args = [*self.command, "exec"]
        if session_id:
            args += ["resume", session_id]
        else:
            args += ["-C", str(self.cwd)]
        args += ["--json", "--skip-git-repo-check", *SANDBOX, "-o", str(out)]
        return [*args, *EFFORT, "-"]

    def ask(self, text: str, now: float | None = None, timeout: float = VOICE_TIMEOUT) -> Turn:
        """1回聞いて、返事を待つ。**止まるので、呼ぶ側は別のスレッドに出す。**"""
        now = time.time() if now is None else now
        session_id = self.session_id(now)
        prompt = text if session_id else f"{INSTRUCTIONS}\n\n---\n\n{text}"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        out = self.state_dir / "last-message.txt"
        out.unlink(missing_ok=True)
        try:
            reply, started, failed = self._run(
                self.build_command(session_id, out), prompt, timeout)
        except FileNotFoundError:
            return Turn(failed="codex が入っていません")
        if started:
            self.remember(started, now)
        if failed:
            return Turn(failed=failed)
        return Turn(text=reply) if reply else Turn(failed="返事が空でした")

    def _run(self, command: list[str], prompt: str, timeout: float) -> tuple[str, str, str]:
        """`codex exec` を1回動かして、（返事、会話の id、うまくいかなかった理由）を返す。

        **stderr は捨てない。** 捨てていたら、`resume` が `-s` を断っていたことに気づけず、
        「返事が空でした」とだけ言う状態になっていた。JSON でない行は出来事として読めないので、
        取っておいて、返事が無かったときの理由に使う。
        """
        said, noise, started, failed = [], [], "", ""
        out = Path(command[command.index("-o") + 1])
        with subprocess.Popen(command, cwd=self.cwd, text=True, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
                for line in proc.stdout:
                    event = _event(line)
                    if event is None:
                        if line.strip():
                            noise.append(line.strip())
                        continue
                    started = str(event.get("thread_id") or "") or started
                    failed = _trouble(event) or failed
                    said.append(_message(event))
                proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, BrokenPipeError):
                proc.kill()
                failed = failed or "返事が返ってきませんでした"
        reply = _last_message(out) or "\n\n".join(t for t in said if t)
        if not reply and not failed and noise:
            log.warning("codex が答えませんでした: %s", " / ".join(noise[:5]))
        return reply, started, failed


def _event(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _message(event: dict) -> str:
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "agent_message":
        return str(item.get("text") or "")
    return ""


def _trouble(event: dict) -> str:
    """上限や失敗を拾う。`item.completed` の error は Codex 自身の警告なので見ない。"""
    if event.get("type") not in ("error", "turn.failed"):
        return ""
    error = event.get("error")
    message = error.get("message") if isinstance(error, dict) else event.get("message")
    text = str(message or "うまく話せませんでした")
    return "Codex の上限に当たったみたい。" if LIMIT.search(text) else text


def _last_message(path: Path) -> str:
    """`-o` に書かれた最後の返事。出来事の形が変わっても、ここは変わらない。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    path.unlink(missing_ok=True)
    return text
