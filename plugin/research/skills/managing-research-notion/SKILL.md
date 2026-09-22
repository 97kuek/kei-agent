---
name: managing-research-notion
description: Use when研究ホームの Notion にメモや結果を残すとき、探すとき、ページやデータベースを整理するとき。
---

# 研究ホームの Notion を使う

`research-notion` の MCP だけを使う。この道具は**研究ホームの下しか触れない**。
外を指すと「研究ホーム外の対象です」で失敗する。

**ほかの Notion 連携や `NOTION_TOKEN` を探さない。** つながらないときは、
「Notion のゲートウェイにつながらない」と返して終わる。回り道は無い。

| したいこと | 道具 |
|---|---|
| 探す | `search` |
| 読む（子ブロックも返る） | `read` |
| データベースの中身を絞って読む | `query` |
| ページを作る | `create_page` |
| プロパティを直す | `update_page` |
| 本文を足す | `append_blocks` |
| ゴミ箱に入れる | `archive` |
| 移す・複製する | `move`、`duplicate` |
| データベースを作る・列を直す | `create_database`、`update_database` |

## 進め方

1. `search` か `query` で、同じ内容のページが無いか先に見る。**同じメモを2つ作らない**
2. 対象の ID を確かめてから、直す道具を呼ぶ
3. 消す・移すのは戻しにくい。何をどう変えるかを1行で言ってから実行する

## 返し方

作った・直したページの名前と、Notion の URL を添える。
失敗したときは、失敗した操作と理由をそのまま伝える（成功したことにしない）。
