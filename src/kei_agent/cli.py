"""`kei-agent` のコマンド。何も付けなければ Kei Agent を動かす（launchd からの起動と同じ）。

- `kei-agent` … Kei Agent（Slack の受け口と定期処理）を動かす
- `kei-agent doctor` … 今の設定と動きを点検する（読むだけ）
"""

from __future__ import annotations

import sys

USAGE = """使い方: kei-agent [コマンド]

  （何も付けない）  Kei Agent を動かす（ふだんは launchd が起動する）
  doctor            今の設定と動きを点検する（読むだけ。--all でうまくいっているものも並べる）
"""


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        from kei_agent import app

        app.main()
        return
    command, rest = argv[0], argv[1:]
    if command == "doctor":
        from kei_agent import doctor

        raise SystemExit(doctor.main(rest))
    if command in ("-h", "--help", "help"):
        print(USAGE)
        return
    print(f"知らないコマンドです: {command}\n\n{USAGE}", file=sys.stderr)
    raise SystemExit(2)
