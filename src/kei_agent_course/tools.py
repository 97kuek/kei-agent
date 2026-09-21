"""大学エージェントの claude に渡す道具（アカウントに付いている連携）。

Box（学部要項・過去問）と Notion（授業・課題）は、どちらも Claude のアカウントに
**すでに連携として繋がっている**。自前で API を書かず、その連携を使う（docs/agents.md）。

- Box は読む道具だけ。アップロード・移動は名指しで断る
- Notion は「課題」を直せるところまで（状態の更新、行の追加）。移動・複製・削除は名指しで断る。
  連携はアカウント全体に届くので、**どこを触ってよいかは道具では絞れない**。範囲は prompts/course.md で縛る
- 連携はログイン（プロファイル）に付いてくるので、仕事用と同じく `CLAUDE_CONFIG_DIR` で
  個人アカウントのプロファイルを指しておく（秘密情報のファイル）
- 触れるのは連携だけ（Bash もファイルも使わせない）ので、作業用ディレクトリを持たない
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

AGENT = "course"
_BOX = "mcp__claude_ai_Box__"
_NOTION = "mcp__claude_ai_Notion__"

# 使わせる道具。Box は読むだけ、Notion は「課題」を直せるところまで
ALLOWED = (
    # Box: 探す・中身を読む・ページを画像で見る（手書きやスキャンの過去問）
    f"{_BOX}search_files_keyword",
    f"{_BOX}search_folders_by_name",
    f"{_BOX}list_folder_content_by_folder_id",
    f"{_BOX}get_file_details",
    f"{_BOX}get_file_content",
    f"{_BOX}get_file_preview",
    f"{_BOX}get_preview_page",
    # Notion: 授業と課題を読む
    f"{_NOTION}notion-search",
    f"{_NOTION}notion-fetch",
    f"{_NOTION}notion-query-data-sources",
    # Notion: 「課題」を直す（「提出済みにして」など）。
    # 連携はアカウント全体に届くので、道具では授業ホームに絞れない。
    # どこを触ってよいかは prompts/course.md で縛り、消す・移す道具は下で断る
    f"{_NOTION}notion-update-page",
    f"{_NOTION}notion-create-pages",
)
# 名指しで断る道具（許可の一覧に入れていなくても、念のため）
DENY = (
    f"{_BOX}upload_file",
    f"{_BOX}upload_file_version",
    f"{_BOX}create_folder",
    f"{_BOX}move_file",
    f"{_BOX}move_folder",
    f"{_BOX}copy_file",
    f"{_BOX}update_file_properties",
    f"{_BOX}update_folder_properties",
    f"{_BOX}set_file_metadata",
    f"{_BOX}create_file_comment",
    f"{_NOTION}notion-create-database",
    f"{_NOTION}notion-move-pages",
    f"{_NOTION}notion-duplicate-page",
    f"{_NOTION}notion-create-comment",
    f"{_NOTION}notion-spawn-session",
)
# claude 1回の上限時間（分）。探して読んで答えるだけなので短くする
TIMEOUT_MINUTES = 5
