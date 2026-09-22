---
name: managing-course-notion
description: Use when授業ホームの Notion のページやデータベースを作る、整理する、移動する、複製する、消すとき。
---

# 授業ホームの Notion を整える

授業ホームの中では、**作成・更新・移動・複製・削除のどれもできる**。

| したいこと | 使う道具 |
|---|---|
| 探す・読む | `notion-search`、`notion-fetch`、`notion-query-data-sources` |
| ページを作る | `notion-create-pages` |
| 中身・プロパティを直す | `notion-update-page` |
| 移す | `notion-move-pages` |
| 複製する | `notion-duplicate-page` |
| 消す（ゴミ箱へ） | `notion-update-page` の `in_trash` |
| データベースを作る・直す | `notion-create-database`、`notion-update-data-source` |

## 気をつけること

- 触れるのは**授業ホームの下だけ**。この Claude には授業ホームしか共有されていないので、
  外のページは見つからない。見つからないものを別の経路で探さない
- 消す・移す・まとめて直すのは、**戻せない**。何をどう変えるかを先に1行で言ってから実行する
- 「授業」「課題」のスキーマ（列）を変えると、Moodle からの取り込みが止まる。
  列を消す・名前を変えるのは、依頼者に確かめてからにする

## 返し方

直したページの名前と URL を並べる。消したものは、ゴミ箱から戻せることも添える。
