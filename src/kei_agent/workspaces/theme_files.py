"""テーマのディレクトリの読み書き: Slack の添付の保存、outputs/ の変化、スレッドのログ。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

# スレッドに添付する outputs/ のファイルの数と大きさの上限
MAX_UPLOADS = 10
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# Slack から受け取って inputs/ に保存する1ファイルの上限
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024

_UNSAFE_FILENAME = re.compile(r"[^\w.\-]+")

Snapshot = dict[Path, tuple[float, int]]


def snapshot_outputs(cwd: Path) -> Snapshot:
    outputs = cwd / "outputs"
    if not outputs.is_dir():
        return {}
    return {
        p: (p.stat().st_mtime, p.stat().st_size)
        for p in outputs.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    }


def changed_files(before: Snapshot, after: Snapshot, since: float | None = None) -> list[Path]:
    return sorted(
        p for p, stat in after.items()
        if before.get(p) != stat or (since is not None and stat[0] >= since)
    )


def split_uploads(files: list[Path]) -> tuple[list[Path], list[Path]]:
    """添付するものと、数か大きさの上限を超えて添付しないものに分ける。"""
    uploadable = [p for p in files if p.stat().st_size <= MAX_UPLOAD_BYTES][:MAX_UPLOADS]
    return uploadable, [p for p in files if p not in uploadable]


def thread_log_path(cwd: Path, thread_ts: str) -> Path:
    return cwd / ".kei-agent" / "threads" / f"{thread_ts}.md"


def append_thread_log(cwd: Path, channel_name: str, thread_ts: str, who: str, text: str) -> None:
    """スレッドのやり取りをテーマのディレクトリにも残す。Codex App から Slack を見ずに経緯を読めるようにする。"""
    path = thread_log_path(cwd, thread_ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        started = datetime.fromtimestamp(float(thread_ts)).strftime("%Y-%m-%d %H:%M")
        path.write_text(f"# #{channel_name} のスレッド（{started} 開始）\n", encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## {who}（{stamp}）\n\n{text.strip()}\n")


def _free_name(inputs: Path, name: str) -> Path:
    dest = inputs / name
    stem, suffix, n = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = inputs / f"{stem}-{n}{suffix}"
        n += 1
    return dest


async def download_files(files: list[dict], cwd: Path, bot_token: str) -> list[str]:
    """Slack の添付を inputs/ に保存し、cwd からの相対パスを返す。同じ名前があれば番号を足す。"""
    saved: list[str] = []
    if not files:
        return saved
    inputs = cwd / "inputs"
    inputs.mkdir(exist_ok=True)
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {bot_token}"}) as http:
        for f in files:
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            if int(f.get("size") or 0) > MAX_DOWNLOAD_BYTES:
                log.warning("大きすぎる添付は保存しません: %s", f.get("name"))
                continue
            name = _UNSAFE_FILENAME.sub("_", f.get("name") or f.get("id") or "file").lstrip(".") or "file"
            dest = _free_name(inputs, name)
            async with http.get(url) as resp:
                resp.raise_for_status()
                # 全部をメモリに載せない
                with dest.open("wb") as out:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        out.write(chunk)
            saved.append(str(dest.relative_to(cwd)))
    return saved
