#!/usr/bin/env python3
"""arXiv と Semantic Scholar で論文を探す小さなCLI（標準ライブラリだけで動く）。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

USER_AGENT = "ezra-literature/0.1"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
S2_FIELDS = "title,authors,year,venue,citationCount,externalIds,url,abstract,openAccessPdf"


def _get(url: str, headers: dict[str, str] | None = None, retries: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    delay = 2.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def _short(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def search_arxiv(query: str, limit: int, sort: str) -> list[dict]:
    params = {
        "search_query": query if ":" in query else " AND ".join(f"all:{w}" for w in query.split()),
        "start": 0,
        "max_results": limit,
        "sortBy": {"relevance": "relevance", "date": "submittedDate"}[sort],
        "sortOrder": "descending",
    }
    root = ET.fromstring(_get("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)))
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        abs_url = entry.findtext(f"{ATOM}id", "")
        arxiv_id = re.sub(r"v\d+$", "", abs_url.rsplit("/abs/", 1)[-1])
        papers.append({
            "id": f"arXiv:{arxiv_id}",
            "title": _short(entry.findtext(f"{ATOM}title"), 300),
            "authors": [a.findtext(f"{ATOM}name", "") for a in entry.findall(f"{ATOM}author")],
            "year": int(entry.findtext(f"{ATOM}published", "0")[:4] or 0),
            "venue": entry.findtext(f"{ARXIV_NS}journal_ref") or "arXiv",
            "citations": None,
            "url": abs_url,
            "abstract": _short(entry.findtext(f"{ATOM}summary"), 1200),
        })
    return papers


def search_s2(query: str, limit: int, year: str | None) -> list[dict]:
    params = {"query": query, "limit": limit, "fields": S2_FIELDS}
    if year:
        params["year"] = year
    headers = {}
    if os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = os.environ["S2_API_KEY"]
    data = json.loads(_get("https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params), headers))
    papers = []
    for p in data.get("data", []):
        ext = p.get("externalIds") or {}
        pid = f"arXiv:{ext['ArXiv']}" if ext.get("ArXiv") else (f"DOI:{ext['DOI']}" if ext.get("DOI") else f"S2:{p.get('paperId')}")
        papers.append({
            "id": pid,
            "title": _short(p.get("title"), 300),
            "authors": [a.get("name", "") for a in p.get("authors") or []],
            "year": p.get("year"),
            "venue": p.get("venue") or "",
            "citations": p.get("citationCount"),
            "url": (p.get("openAccessPdf") or {}).get("url") or p.get("url"),
            "abstract": _short(p.get("abstract"), 1200),
        })
    return papers


def known_ids(papers_dir: Path) -> dict[str, str]:
    """papers/*.md の先頭にある `id:` を集める。"""
    ids = {}
    for md in sorted(papers_dir.glob("*.md")):
        head = md.read_text(encoding="utf-8").split("\n---", 1)[0]
        m = re.search(r"^id:\s*(.+)$", head, re.MULTILINE)
        if m:
            ids[m.group(1).strip().strip("\"'")] = md.name
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lit")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="論文を探す")
    p.add_argument("query")
    p.add_argument("--source", choices=["arxiv", "s2"], default="s2")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--sort", choices=["relevance", "date"], default="relevance", help="arxiv のみ")
    p.add_argument("--year", help="s2 のみ。例: 2023- / 2020-2024")

    p = sub.add_parser("known", help="papers/ に保存済みの論文IDを表示する")
    p.add_argument("--dir", default="papers")

    args = parser.parse_args(argv)
    if args.command == "known":
        print(json.dumps(known_ids(Path(args.dir)), ensure_ascii=False, indent=2))
        return 0

    limit = max(1, min(args.limit, 50))
    try:
        papers = search_arxiv(args.query, limit, args.sort) if args.source == "arxiv" else search_s2(args.query, limit, args.year)
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
        hint = "混雑しています。少し待つか、別のソースを使ってください" if getattr(e, "code", None) == 429 else "取得に失敗しました"
        print(json.dumps({"error": f"{args.source}: {hint} ({e})"}, ensure_ascii=False), file=sys.stderr)
        return 1
    saved = known_ids(Path("papers")) if Path("papers").is_dir() else {}
    for paper in papers:
        paper["saved_as"] = saved.get(paper["id"])
    print(json.dumps(papers, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
