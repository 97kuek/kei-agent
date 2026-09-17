---
name: literature
description: 先行研究や関連論文を arXiv と Semantic Scholar で探し、要約して papers/ に保存する。「先行研究を調べて」「関連論文を探して」「この論文の周辺を整理して」などの依頼で使う。
---

# 文献調査

## 探す

```bash
# Semantic Scholar（引用数と出版年つき。年で絞れる）
python3 "$EZRA_PLUGIN_DIR/skills/literature/scripts/lit.py" search "vision language model counting" --source s2 --limit 20 --year 2023-
# arXiv（新しいプレプリント。--sort date で新しい順）
python3 "$EZRA_PLUGIN_DIR/skills/literature/scripts/lit.py" search "vision language model counting" --source arxiv --sort date --limit 20
# 保存済みの論文
python3 "$EZRA_PLUGIN_DIR/skills/literature/scripts/lit.py" known
```

- 検索語を2〜3通り変え、両方のソースを使う。Semantic Scholar が 429 を返し続けるときは arXiv と Web検索で補う
- 結果の `saved_as` が入っている論文は保存済み。読み直さず、そのファイルを使う
- 要旨だけで判断できないときは、WebFetch で本文（arXiv の abs ページなど）を確認する

## 保存する

依頼に関係すると判断した論文だけを、1本1ファイルで `papers/` に保存する。ファイル名は ID をもとにする（例: `papers/arXiv-2406.12345.md`）。

```markdown
---
id: arXiv:2406.12345
title: 論文タイトル
authors: [First Author, Second Author]
year: 2024
venue: CVPR
citations: 120
url: https://arxiv.org/abs/2406.12345
found_for: 調べたときの依頼を一言で
---

## 要点
- 何を問い、何をして、何が分かったか（3行程度）

## この研究との関係
- テーマの `CLAUDE.md` の前提に照らして、使える点・違う点
```

## 返答

- 見つかった本数、主な流れ（2〜4個のグループ）、特に重要な論文3本程度とその理由をまとめる
- 論文には必ずURLを添える。要旨しか読んでいない論文は、そう書く
