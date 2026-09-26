"""起動スクリプト（deploy/_common.sh）が、Python の本体を起動する前に知りたい場所を出す。

使い方: python -m kei_agent.paths secrets   秘密情報の置き場所（config.toml の [paths] secrets）
"""

from __future__ import annotations

import sys

from kei_agent.config import ConfigError, load_config, user_home


def main() -> None:
    if sys.argv[1:] != ["secrets"]:
        sys.exit("使い方: python -m kei_agent.paths secrets")
    try:
        print(load_config().secrets_dir)
    except ConfigError:
        # 設定が読めなくても、既定の場所なら秘密情報を読める（本体は起動したところで設定の誤りを知らせる）
        print(user_home() / "secrets")


if __name__ == "__main__":
    main()
