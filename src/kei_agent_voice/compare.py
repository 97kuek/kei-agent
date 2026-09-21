"""声を聴き比べる（`kei-agent-voice-compare`）。

VOICEVOX と、その API に合わせたエンジン（AivisSpeech など）を同じ文で鳴らし、wav に落とす。
「人工合成感」は数字で比べられないので、耳で決めるための道具（docs/voice.md の9節）。

使い方:
    kei-agent-voice-compare                      # 動いているエンジンを探して、見本の文で鳴らす
    kei-agent-voice-compare --port 10101         # 探す先を足す
    kei-agent-voice-compare --text "好きな文"     # 文を替える
    kei-agent-voice-compare --speakers           # そのエンジンの話者の一覧を出す
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kei_agent_voice.speech import DEFAULT_SPEED, Voicevox

# 探しにいく待ち受け口。VOICEVOX は 50021。ほかのエンジンは `--port` で足す
KNOWN_PORTS = (50021, 10101)
# 声の質は、長さと言い回しで印象が変わる。実際に Kei Agent が喋る形に近いものを並べる
SAMPLES = (
    "うん、聞いてるよ。",
    "さっき amr-query に頼んだ作業、終わったよ。結果は Slack に出てる。",
    "明日の2限はデータベース、4限が情報通信ネットワークBだよ。レポートの締切は明日の夕方までね。",
)
OUT_DIR = Path.home() / "kei-agent" / "overview" / "voice" / "compare"


def running(ports: list[int], host: str) -> list[tuple[int, str]]:
    """待ち受けているエンジンと、その名乗り（バージョン）。"""
    found = []
    for port in ports:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/version", timeout=2) as resp:
                found.append((port, resp.read().decode("utf-8", "replace").strip().strip('"')))
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return found


def speakers(host: str, port: int) -> list[tuple[int, str]]:
    return Voicevox(host, port).speakers()


def main() -> None:
    parser = argparse.ArgumentParser(prog="kei-agent-voice-compare", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, action="append", default=[],
                        help="探す待ち受け口を足す（既定は 50021 と 10101）")
    parser.add_argument("--speaker", type=int, action="append", default=[],
                        help="鳴らす話者。書かなければ、そのエンジンの先頭の話者")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    parser.add_argument("--text", action="append", default=[], help="鳴らす文。書かなければ見本の文")
    parser.add_argument("--speakers", action="store_true", help="話者の一覧を出して終わる")
    args = parser.parse_args()

    ports = list(dict.fromkeys([*KNOWN_PORTS, *args.port]))
    found = running(ports, args.host)
    if not found:
        sys.exit(f"待ち受けているエンジンがありません（見た口: {', '.join(str(p) for p in ports)}）。\n"
                 "VOICEVOX か AivisSpeech を立ち上げてから、もう一度実行してください。")

    if args.speakers:
        for port, version in found:
            print(f"\n== {args.host}:{port}（{version}）==")
            for sid, name in speakers(args.host, port):
                print(f"  {sid:>12}  {name}")
        return

    texts = args.text or list(SAMPLES)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for port, version in found:
        ids = args.speaker or [speakers(args.host, port)[0][0]]
        for sid in ids:
            engine = Voicevox(args.host, port, speaker=sid, speed=args.speed)
            for i, text in enumerate(texts, 1):
                path = OUT_DIR / f"{port}-{sid}-{i}.wav"
                path.write_bytes(engine.synthesize(text))
                made.append(path)
            print(f"{args.host}:{port}（{version}）話者 {sid}: {len(texts)} 本")

    print(f"\n{len(made)} 本を {OUT_DIR} に書きました。続けて鳴らします（Ctrl-C で止める）。")
    for path in made:
        print(f"  {path.name}")
        subprocess.run(["afplay", str(path)], check=False)


if __name__ == "__main__":
    main()
