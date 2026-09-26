"""RSS・Atom・arXiv と、記事の本文を読む（AI は使わない）。

どれも外から来た文なので、読む大きさと待つ時間に上限を置き、http(s) だけを読む。
ここで読んだ文は、Web を使えない AI の回にだけ渡す（docs/architecture.md の「知識」）。
"""

from __future__ import annotations

import email.utils
import html
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (Kei Agent reader)"
TIMEOUT_SECONDS = 15
# 1回に読む大きさの上限。フィードは全文入りのもの（Vercel など）があるので大きめ
MAX_BYTES = 10 * 1024 * 1024
ARTICLE_BYTES = 3 * 1024 * 1024
# AI に渡す本文の長さの上限（文字）
ARTICLE_CHARS = 6000
# 説明文の長さの上限（文字。候補の一覧に並べる）
SUMMARY_CHARS = 400
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_TIMEOUT_SECONDS = 30
# arXiv は混んでいるときや立て続けに読んだときに、しばらく断る（406・429・5xx。本文は空）。
# 朝に1テーマ1回しか読まないので、間をだんだん長くあけて、やり直す（秒）
ARXIV_WAITS = (5.0, 15.0, 45.0)
_BUSY_CODES = {406, 429, 500, 502, 503, 504}
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
# 情報源の書き方（「収集」ページ）: `zenn: llm, ai` と `qiita: llm` はトピック・タグごとのフィード
SHORTHANDS = {
    "zenn": ("Zenn", "https://zenn.dev/topics/{}/feed"),
    "qiita": ("Qiita", "https://qiita.com/tags/{}/feed"),
}
# よく使うブログの表示名（無ければドメイン名）
SOURCE_NAMES = {
    "openai.com": "OpenAI", "huggingface.co": "Hugging Face", "research.google": "Google Research",
    "blog.google": "Google", "deepmind.google": "Google DeepMind", "github.blog": "GitHub",
    "www.microsoft.com": "Microsoft Research", "blog.cloudflare.com": "Cloudflare", "vercel.com": "Vercel",
}
_SPACES = re.compile(r"\s+")


class FetchError(RuntimeError):
    """読めなかった（つながらない、大きすぎる、http(s) ではない）。"""


class _Busy(FetchError):
    """一時的に読めなかった（混んでいる、つながらない、途中で切れた）。時間をおけば読めるかもしれない。"""


@dataclass(frozen=True)
class Source:
    url: str
    name: str
    # Zenn・Qiita のトピック（そのトピックの興味に合うとみなす）。ブログなら空
    topic: str = ""


@dataclass(frozen=True)
class Entry:
    title: str
    url: str
    summary: str
    source: str
    published: datetime | None = None
    topic: str = ""


@dataclass(frozen=True)
class Paper:
    id: str                 # arXiv:2406.12345
    title: str
    url: str
    abstract: str
    published: datetime | None = None
    authors: tuple[str, ...] = field(default_factory=tuple)
    venue: str = ""

    @property
    def year(self) -> str:
        return str(self.published.year) if self.published else ""


def expand_sources(lines: list[str]) -> list[Source]:
    """「収集」ページの情報源の行を、読むフィードにする。書き方が分からない行は飛ばす。"""
    found: dict[str, Source] = {}
    for line in lines:
        text = line.strip()
        head, sep, rest = text.partition(":")
        key = head.strip().lower()
        if sep and key in SHORTHANDS and not rest.startswith("//"):
            name, pattern = SHORTHANDS[key]
            for topic in re.split(r"[,、]", rest):
                topic = topic.strip()
                if topic:
                    url = pattern.format(urllib.parse.quote(topic.lower()))
                    found.setdefault(url, Source(url, name, topic))
        elif text.startswith(("http://", "https://")):
            host = urllib.parse.urlsplit(text).netloc
            found.setdefault(text, Source(text, SOURCE_NAMES.get(host, host.removeprefix("www."))))
    return list(found.values())


def fetch(url: str, timeout: float = TIMEOUT_SECONDS, limit: int = MAX_BYTES, waits: tuple[float, ...] = ()) -> bytes:
    """http(s) のページを、上限の大きさまで読む。waits は、一時的に読めなかったときに、やり直すまで待つ秒数。"""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise FetchError(f"http(s) ではありません: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for wait in waits:
        try:
            return _read(request, timeout, limit)
        except _Busy:
            time.sleep(wait)
    return _read(request, timeout, limit)


def _read(request: urllib.request.Request, timeout: float, limit: int) -> bytes:
    url = request.full_url
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as e:
        raise (_Busy if e.code in _BUSY_CODES else FetchError)(f"{url} を読めません: {e}") from None
    except (OSError, http.client.HTTPException) as e:
        raise _Busy(f"{url} を読めません: {e}") from None
    except ValueError as e:
        raise FetchError(f"{url} を読めません: {e}") from None
    if len(data) > limit:
        raise FetchError(f"{url} が大きすぎます")
    return data


def _text(value: str | None, limit: int = SUMMARY_CHARS) -> str:
    """HTML のタグと余分な空白を外して、長さを切る。"""
    plain = html_text(value or "") if "<" in (value or "") else html.unescape(value or "")
    plain = _SPACES.sub(" ", plain).strip()
    return plain if len(plain) <= limit else plain[:limit - 1] + "…"


def _date(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_feed(data: bytes, source: Source) -> list[Entry]:
    """RSS 2.0 か Atom を読む。読めなければ空。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    entries = []
    if root.tag == "rss" or root.find("channel") is not None:
        for item in root.iter("item"):
            url = (item.findtext("link") or "").strip()
            title = _text(item.findtext("title"), 200)
            if url and title:
                entries.append(Entry(title, url, _text(item.findtext("description")), source.name,
                                     _date(item.findtext("pubDate")), source.topic))
    elif root.tag == f"{ATOM}feed":
        for item in root.iter(f"{ATOM}entry"):
            links = item.findall(f"{ATOM}link")
            link = next((x for x in links if x.get("rel", "alternate") == "alternate"), links[0] if links else None)
            url = (link.get("href") if link is not None else "") or ""
            title = _text(item.findtext(f"{ATOM}title"), 200)
            if url and title:
                summary = item.findtext(f"{ATOM}summary") or item.findtext(f"{ATOM}content")
                published = item.findtext(f"{ATOM}published") or item.findtext(f"{ATOM}updated")
                entries.append(Entry(title, url, _text(summary), source.name, _date(published), source.topic))
    return entries


def arxiv_papers(keywords: list[str], limit: int = 50) -> list[Paper]:
    """キーワードのどれかを含む新しい論文（読めなければ FetchError）。"""
    return parse_arxiv(fetch(arxiv_url(keywords, limit), timeout=ARXIV_TIMEOUT_SECONDS, waits=ARXIV_WAITS))


def arxiv_url(keywords: list[str], limit: int = 50) -> str:
    """キーワードのどれかを含む新しい論文から順に（arXiv の API）。"""
    terms = " OR ".join(f'all:"{k}"' if " " in k else f"all:{k}" for k in keywords if k.strip())
    query = urllib.parse.urlencode({"search_query": terms, "sortBy": "submittedDate",
                                    "sortOrder": "descending", "max_results": limit})
    return f"{ARXIV_API}?{query}"


def parse_arxiv(data: bytes) -> list[Paper]:
    """arXiv の API の返事（Atom）を論文にする。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    papers = []
    for item in root.iter(f"{ATOM}entry"):
        abs_url = (item.findtext(f"{ATOM}id") or "").strip()
        number = re.sub(r"v\d+$", "", abs_url.rsplit("/abs/", 1)[-1]) if "/abs/" in abs_url else ""
        title = _text(item.findtext(f"{ATOM}title"), 300)
        if not number or not title:
            continue
        authors = tuple(_text(a.findtext(f"{ATOM}name"), 80) for a in item.findall(f"{ATOM}author"))
        papers.append(Paper(f"arXiv:{number}", title, f"https://arxiv.org/abs/{number}",
                            _text(item.findtext(f"{ATOM}summary"), 2000),
                            _date(item.findtext(f"{ATOM}published")), authors,
                            _text(item.findtext(f"{ARXIV}journal_ref"), 200)))
    return papers


class _Text(HTMLParser):
    """本文らしいところの文字だけを集める。article か main があれば、その中だけ。"""

    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"}
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "br", "section", "pre", "tr"}

    def __init__(self):
        super().__init__()
        self.skipping = 0
        self.inside = 0
        self.seen_main = False
        self.all_text: list[str] = []
        self.main_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in ("article", "main"):
            self.inside += 1
            self.seen_main = True
        if tag in self.BLOCK:
            self._add("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag in ("article", "main") and self.inside:
            self.inside -= 1

    def handle_data(self, data):
        if not self.skipping:
            self._add(data)

    def _add(self, text: str) -> None:
        self.all_text.append(text)
        if self.inside:
            self.main_text.append(text)


def html_text(page: str) -> str:
    parser = _Text()
    parser.feed(page)
    parser.close()
    chosen = parser.main_text if parser.seen_main and "".join(parser.main_text).strip() else parser.all_text
    lines = (_SPACES.sub(" ", line).strip() for line in "".join(chosen).splitlines())
    return "\n".join(line for line in lines if line)


def article_text(url: str, limit: int = ARTICLE_CHARS) -> str:
    """記事のページの本文（読めなければ FetchError）。"""
    data = fetch(url, limit=ARTICLE_BYTES)
    text = html_text(data.decode("utf-8", "replace"))
    return text if len(text) <= limit else text[:limit] + "…"
