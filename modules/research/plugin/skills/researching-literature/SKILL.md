---
name: researching-literature
description: Use when先行研究や関連論文を探すとき、ある論文の周辺を整理するとき、見つけた論文を研究ホームの先行研究 DB に残すとき。
---

# 文献調査

このファイルのあるディレクトリ（skill を読み込んだときに示される base directory）を `$SKILL` とする。

## 探す

```bash
# Semantic Scholar（引用数と出版年つき。年で絞れる）
python3 "$SKILL/scripts/lit.py" search "vision language model counting" --source s2 --limit 20 --year 2023-
# arXiv（新しいプレプリント。--sort date で新しい順）
python3 "$SKILL/scripts/lit.py" search "vision language model counting" --source arxiv --sort date --limit 20
```

- 検索語を2〜3通り変え、両方のソースを使う。Semantic Scholar が 429 を返し続けるときは arXiv と Web検索で補う
- 保存済みかは、研究ホームの「先行研究」DB を `kei-notion` の `query` で ID（`arXiv:2406.12345` の形）を指定して確かめる。
  保存済みなら読み直さず、その行の要点と「この研究との関係」を使う
- 要旨だけで判断できないときは、WebFetch で本文（arXiv の abs ページなど）を確認する
- 毎朝の新着は知識の担当が同じ DB に入れている（出どころ「毎朝の新着」）

## 保存する

依頼に関係すると判断した論文だけを、研究ホームの「先行研究」DB に1本1行で入れる（`kei-notion` の `create_page`、
親は「先行研究」のデータソース。見つからなければ `search` で「先行研究」）。手元に `papers/` は作らない。

| 列 | 入れるもの |
|---|---|
| 名前 | 論文のタイトル |
| ID | `arXiv:2406.12345`、DOI、Semantic Scholar の ID のどれか（同じ論文を二度入れないための鍵） |
| URL、著者、年、会場 | 分かる範囲で（プレプリントなら会場は空） |
| 要点 | 何を問い、何をして、何が分かったか（3行程度） |
| この研究との関係 | テーマの `CLAUDE.md` の前提に照らして、使える点・違う点 |
| 見つけた日 | 今日 |
| 出どころ | 「依頼」 |
| 状態 | 「未読」（読んだら「読んだ」、使うなら「使う」） |
| テーマ | このテーマの行（「テーマ」DB を名前で `query`） |

同じ ID の行がすでにあれば、新しく作らず、テーマが付いていなければ `update_page` でテーマを足す。

## 返答

- 見つかった本数、主な流れ（2〜4個のグループ）、特に重要な論文3本程度とその理由をまとめる
- 論文には必ずURLを添える。要旨しか読んでいない論文は、そう書く
