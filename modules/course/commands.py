"""大学のモジュールの、手で動かすコマンド（`kei-agent-module course <コマンド> [引数...]`）。

- setup … 授業ホームに DB を作る（`--seed 年度` で、秋学期の履修科目を入れる）
- sync … Moodle の締切を「課題」に取り込む（`--days`、`--all`）
- inspect … Moodle の科目と「授業」を見比べる（読むだけ）
- academic-import … 成績の HTML を読んで Notion に入れる（`--dry-run` / `--apply`）
"""

from __future__ import annotations

from . import academic_sync, catalog, notion_setup, notion_sync

COMMANDS = {
    "setup": notion_setup.main,
    "sync": notion_sync.main,
    "inspect": catalog.main,
    "academic-import": academic_sync.main,
}
