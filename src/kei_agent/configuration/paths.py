"""起動スクリプト（deploy/_common.sh）が、Python の本体を起動する前に知りたい場所を出す。

使い方: python -m kei_agent.configuration.paths secrets   秘密情報の置き場所（config.toml の [paths] secrets）
        python -m kei_agent.configuration.paths hands     MCP の住所とトンネルの番号（[hands] の url と tunnel。1行ずつ）
"""

from __future__ import annotations

import sys

from kei_agent.configuration.config import ConfigError, load_config, user_home


def main() -> None:
    asked = sys.argv[1:]
    if asked == ["hands"]:
        # 設定が読めなければ、トンネルは起こさない（本体が設定の誤りを知らせる）
        try:
            config = load_config()
        except ConfigError as e:
            sys.exit(str(e))
        print(config.hands_url.rstrip("/"))
        print(config.hands_tunnel)
        return
    if asked != ["secrets"]:
        sys.exit("使い方: python -m kei_agent.configuration.paths secrets | hands")
    try:
        print(load_config().secrets_dir)
    except ConfigError:
        # 設定が読めなくても、既定の場所なら秘密情報を読める（本体は起動したところで設定の誤りを知らせる）
        print(user_home() / "secrets")


if __name__ == "__main__":
    main()
