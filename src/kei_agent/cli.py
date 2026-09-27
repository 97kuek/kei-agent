"""`kei-agent` のコマンド。何も付けなければ Kei Agent を動かす（launchd からの起動と同じ）。

- `kei-agent` … Kei Agent（Slack の受け口と定期処理）を動かす
- `kei-agent setup` … はじめの設定（対話。設定・プロフィール・秘密情報のファイルを作る）
- `kei-agent doctor` … 今の設定と動きを点検する（読むだけ）
- `kei-agent manifest` … オンにしたモジュールに合わせた Slack App の manifest を出す
- `kei-agent module list / add / remove / new / test` … モジュールを一覧にする・足す・外す・作る・テストする
"""

from __future__ import annotations

import sys

USAGE = """使い方: kei-agent [コマンド]

  （何も付けない）  Kei Agent を動かす（ふだんは launchd が起動する）
  setup             はじめの設定（対話。設定・プロフィール・秘密情報のファイルを作る。もうあるものは書き換えない）
  doctor            今の設定と動きを点検する（読むだけ。--all でうまくいっているものも並べる）
  manifest          オンにしたモジュールに合わせた Slack App の manifest を出す（Slack の App Manifest に貼る）
  module            モジュールを一覧にする（list）・足す（add <名前>）・外す（remove <名前>）・
                    ひな形を作る（new <名前> [--ai] [--process]）・テストする（test <名前>）
"""


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        from kei_agent import app

        app.main()
        return
    command, rest = argv[0], argv[1:]
    if command == "setup":
        from kei_agent import setup_command

        raise SystemExit(setup_command.main(rest))
    if command == "doctor":
        from kei_agent import doctor

        raise SystemExit(doctor.main(rest))
    if command == "manifest":
        from kei_agent import slack_manifest

        raise SystemExit(slack_manifest.main(rest))
    if command == "module":
        from kei_agent import module_command

        raise SystemExit(module_command.main(rest))
    if command in ("-h", "--help", "help"):
        print(USAGE)
        return
    print(f"知らないコマンドです: {command}\n\n{USAGE}", file=sys.stderr)
    raise SystemExit(2)
