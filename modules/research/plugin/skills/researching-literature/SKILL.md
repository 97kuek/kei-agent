---
name: researching-literature
description: Use when先行研究や関連論文を探すとき、ある論文の周辺を整理するとき、見つけた論文をテーマのページの先行研究 DB に残すとき。
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
- 保存済みかは、そのテーマのページの「先行研究」DB を `kei-notion` の `query` で Link（arXiv は `https://arxiv.org/abs/<番号>`、バージョン番号は外す）を指定して確かめる。保存済みなら読み直さず、その行の Summary を使う
- 要旨だけで判断できないときは、WebFetch で本文（arXiv の abs ページなど）を確認する

## 保存する

依頼に関係すると判断した論文だけを、そのテーマのページの「先行研究」DB に1本1行で入れる（`kei-notion` の `create_page`、親はその DB のデータソース。テーマのページは「テーマ」DB を Name で `query` して開く）。手元に `papers/` は作らない。

| 列 | 入れるもの |
|---|---|
| Title | 論文のタイトル |
| Status | Unread（読んだら Read、使うなら Use） |
| Summary | 何を問い、何をして、何が分かったか（2〜3文）と、テーマのページの「前提とキーワード」に照らした関係（1文） |
| Link | 論文のページの URL（同じ論文を二度入れないための鍵） |

## 返答

- 見つかった本数、主な流れ（2〜4個のグループ）、特に重要な論文3本程度とその理由をまとめる
- 論文には必ずURLを添える。要旨しか読んでいない論文は、そう書く
