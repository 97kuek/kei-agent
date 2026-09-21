"""遅い道。手元で答えられないことを Codex に相談する（docs/voice.md の6節）。

相談相手を Codex にするのは、契約の枠を Slack の Kei Agent（Claude）と分けるため。
声で長く話した日に、研究の作業が止まらない。

**実測（2026-09-21、`codex exec resume`）**

- プロセスを起こして会話を戻すまで **0.17秒**。残りは全部モデルの往復（短い返事で約5秒）
- 返事は**途中では出てこない**。`item.completed` で丸ごと届く

設計時は「1往復ごとにプロセスを起こすと時間が乗る」と考えて会話を開いたままにするつもりだったが、
測ると開き直しは 0.17 秒しかかからない。**毎回 `codex exec resume` でよい**（常駐プロセスを
抱えなくてよく、落ちても次の問いで勝手に戻る）。

いっぽう返事が丸ごとしか来ないので、「文ができた端から喋る」はできない。
**5秒の沈黙は相槌で埋めるしかない**（`session.py`）。

Codex には読ませるだけにする（`-s read-only`）。実行するのは Slack の Kei Agent だけ。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from kei_agent import themes

log = logging.getLogger(__name__)

# 読ませるだけにする（実行するのは Slack の Kei Agent だけ）。
# `-s read-only` と同じことだが、`resume` が `-s` を受け付けないので設定の形で渡す
SANDBOX = ("-c", 'sandbox_mode="read-only"')
# ふだんは速く、「じっくり考えて」と言った回だけ深く考える
FAST_ARGS = ("-c", "model_reasoning_effort=low")
DEEP_ARGS = ("-c", "model_reasoning_effort=high")
DEEP_WORDS = ("じっくり", "よく考え", "深く考え", "本気で考え")

# 会話の区切り（docs/voice.md の6節）。日付が変わるか、これだけ離れたら新しい会話にする
GAP_SECONDS = 2 * 3600
# 返事から拾う行。これ以外は読み上げる本文
ASK_MARK = "🛠 依頼:"
ASIDE_MARK = "🗣 ひとこと:"
# 追いかけて喋ることが無いときに Codex が書く印
NOTHING = "-"
# 声で聞くので、長い返事は最後まで聞けない
LIMIT = re.compile(r"usage limit", re.IGNORECASE)

INSTRUCTIONS = f"""あなたは Kei Agent。依頼者の分身で、机の上のロボットの声として話す。

- **話し言葉で、3文以内。** 読み上げるので、箇条書き・記号・URL・コードは書かない
- 分からないことは「分からない」と言う。推測で埋めない
- 固有名詞は音声認識で崩れて届く（`amr-query` が「アムルクエリー」、`Slack` が「スラック」）。
  `~/research/` のフォルダ名から推し量って読み替える
- ファイルは読んでよい。**実行や書き込みはしない**（作業するのは Slack の Kei Agent）

作業を頼むべきだと思ったら、本文とは別に最後の行にこう書く（依頼者に読み上げて確認してから渡す）。

{ASK_MARK} <テーマ名> / <依頼の文>

例: {ASK_MARK} amr-query / 条件Bの学習曲線を描いて
"""

# 速い道で即答したあと、追いかけて喋るかどうかを聞くときの前置き
FOLLOW_UP = """依頼者の問いには、手元のデータからすでにこう答えた。

問い: {asked}
答えた: {answered}

付け足すことがあれば、1文だけ次の形で書く。無ければ「{nothing}」だけ書く。

{aside} <付け足す1文>
"""


@dataclass(frozen=True)
class Ask:
    """Codex が「これは作業として頼むべき」と判断したもの。"""
    theme: str
    text: str

    @property
    def spoken(self) -> str:
        """読み上げて確認する文（docs/voice.md の8節）。"""
        return f"{self.theme} に、{self.text}、って頼むよ。いい？"


@dataclass
class Turn:
    """1回のやりとりの結果。"""
    text: str = ""
    ask: Ask | None = None
    aside: str = ""
    failed: str = ""


def wants_deep(text: str) -> bool:
    return any(word in text for word in DEEP_WORDS)


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def parse(reply: str) -> Turn:
    """返事を、読み上げる本文と、印のついた行に分ける。"""
    body, ask, aside = [], None, ""
    for line in reply.splitlines():
        stripped = line.strip()
        if stripped.startswith(ASK_MARK):
            theme, _, text = stripped[len(ASK_MARK):].strip().partition("/")
            if theme.strip() and text.strip():
                # 番号つきの名前（`10_amr-query`）でも渡せるが、読み上げるので番号は落とす
                ask = Ask(themes.theme_name(theme.strip().lstrip("#")), text.strip())
        elif stripped.startswith(ASIDE_MARK):
            aside = stripped[len(ASIDE_MARK):].strip()
        else:
            body.append(line)
    return Turn(text="\n".join(body).strip(), ask=ask, aside=aside)


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

    def forget(self) -> None:
        """「新しく話そう」と言われたとき。"""
        self.session_path.unlink(missing_ok=True)

    # やりとり

    def build_command(self, session_id: str | None, deep: bool, out: Path) -> list[str]:
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
        return [*args, *(DEEP_ARGS if deep else FAST_ARGS), "-"]

    def ask(self, text: str, now: float | None = None, deep: bool | None = None,
            timeout: float = 120.0) -> Turn:
        """1回話しかけて、返事を待つ。**止まるので、呼ぶ側は別のスレッドに出す。**"""
        now = time.time() if now is None else now
        session_id = self.session_id(now)
        prompt = text if session_id else f"{INSTRUCTIONS}\n\n---\n\n{text}"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        out = self.state_dir / "last-message.txt"
        out.unlink(missing_ok=True)
        try:
            reply, started, failed = self._run(
                self.build_command(session_id, wants_deep(text) if deep is None else deep, out),
                prompt, timeout)
        except FileNotFoundError:
            return Turn(failed="codex が入っていません")
        if started:
            self.remember(started, now)
        if failed:
            return Turn(failed=failed)
        turn = parse(reply)
        return turn if turn.text or turn.ask else Turn(failed="返事が空でした")

    def _run(self, command: list[str], prompt: str, timeout: float) -> tuple[str, str, str]:
        """`codex exec` を1回動かして、（返事、会話の id、うまくいかなかった理由）を返す。

        **stderr は捨てない。** 捨てていたら、`resume` が `-s` を断っていたことに気づけず、
        「返事が空でした」とだけ喋る状態になっていた。JSON でない行は出来事として読めないので、
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
