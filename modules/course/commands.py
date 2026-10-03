"""大学のモジュールの、手で動かすコマンド（`kei-agent-module course <コマンド> [引数...]`）。

- setup … 授業ホームに DB を作る（`--seed ファイル` で、履修科目を入れる。書き方は courses.example.toml）
- sync … Moodle の締切を「課題」に取り込む（`--days`、`--all`）
- sync-submissions … 提出・受験終了を「課題」に反映する
- inspect … Moodle の科目と「授業」を見比べる（読むだけ）
- academic-import … 成績のファイルを学校の部品で読んで Notion に入れる（`--dry-run` / `--apply`）
"""

from __future__ import annotations

from . import academic_sync, catalog, notion_setup, notion_sync, submissions

COMMANDS = {
    "setup": notion_setup.main,
    "sync": notion_sync.main,
    "sync-submissions": submissions.main,
    "inspect": catalog.main,
    "academic-import": academic_sync.main,
}
