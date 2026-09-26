---
name: managing-research-notion
description: Use when研究ホームの Notion にメモや結果を残すとき、探すとき、ページやデータベースを整理するとき。
---

# 研究ホームの Notion を使う

`kei-notion`（ゲートウェイ）の MCP だけを使う。この道具は**研究ホームの下しか触れない**。
外を指すと「届きません」で失敗する。

**ほかの Notion 連携や Notion の鍵を探さない。** つながらないときは、
「Notion のゲートウェイにつながらない」と返して終わる。回り道は無い。

| したいこと | 道具 |
|---|---|
| 探す | `search`（`filter` は `page` か `data_source`） |
| 読む（ページはプロパティと Markdown の本文。長ければ `cursor` で続き） | `read` |
| ブロック ID を知る | `read` の `blocks=true` |
| データベースの中身を絞って読む | `query` |
| ページ・DB の行を作る（本文は Markdown） | `create_page`（親はページかデータソース） |
| プロパティ・アイコンを直す、ゴミ箱に入れる | `update_page`（`in_trash`） |
| 本文を足す | `append_blocks`（`after` でブロックの直後） |
| 本文を丸ごと書き直す | `replace_content` |
| ブロックを直す・消す | `update_block`、`delete_block` |
| 移す | `move`（先はページかデータソース） |
| データベースを作る・列を直す | `create_database`、`update_data_source` |

複製の道具は無い（Notion の API に無い）。要るときは `read` した中身で `create_page` する。

## 進め方

1. `search` か `query` で、同じ内容のページが無いか先に見る。**同じメモを2つ作らない**
2. 対象の ID を確かめてから、直す道具を呼ぶ
3. 消す・移す・書き直すのは戻しにくい。何をどう変えるかを1行で言ってから実行する

## 返し方

作った・直したページの名前と、Notion の URL を添える。
失敗したときは、失敗した操作と理由をそのまま伝える（成功したことにしない）。
