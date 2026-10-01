"""既存のフォルダを使うテーマ・プロジェクトの置き場所（利用者のフォルダの themes.toml）と、チャンネル名の決まり。

themes.toml は利用者の設定ファイルなので、設定の領域に置く（読むときに理由を出して止めるのも、設定を読むところ）。
チャンネルから作業場を決めるのは workspaces.themes。
"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kei_agent.configuration.config import Config

log = logging.getLogger(__name__)

# 既存のフォルダを使うテーマの一覧（利用者のフォルダの中）
THEMES_FILE = "themes.toml"

# macOS の保護フォルダ（launchd から読めない。使うにはフルディスクアクセスと、設定の allow_protected_folders）
PROTECTED_FOLDERS = ("Documents", "Desktop", "Downloads")

# iCloud Drive の場所（同期で消えたり戻ったりする）
ICLOUD_DRIVE = Path("~/Library/Mobile Documents").expanduser()

# Slack のチャンネル名は日本語も使えるので、パスとして危ない形だけを弾く
_SAFE_NAME = re.compile(r"^[^./_\\\x00][^/\\\x00]{0,79}$")
# チャンネル名の先頭の番号（`1-amr-query` の `1-`。1〜2桁と -）。並び順のためのもので、名前の一部として扱わない
_NUMBER_PREFIX = re.compile(r"^\d{1,2}-")


def theme_name(channel_name: str) -> str:
    """チャンネル名から、テーマの名前（フォルダ名、Notion のテーマ名）を作る。

    Slack では並び順のために `1-amr-query` のような番号を付ける。番号を変えても
    同じテーマを指し続けられるよう、先頭の番号は外して扱う。
    """
    return _NUMBER_PREFIX.sub("", channel_name)


# 既存のフォルダを使うテーマ（themes.toml）

class PlaceError(ValueError):
    """テーマに使えないフォルダ（無い、Kei Agent 自身の場所、保護フォルダなど）。理由は本文。"""


_places: dict[Path, tuple[float, dict[str, Path]]] = {}


def themes_file(config: Config) -> Path | None:
    return config.user_dir / THEMES_FILE if config.user_dir is not None else None


def places(config: Config) -> dict[str, Path]:
    """themes.toml の、既存のフォルダを使うテーマ（テーマの名前 → フォルダ）。変わっていれば読み直す。

    使えないものは飛ばしてログに残す（依頼を止めない。設定を読むときは check_places が理由を出して止める）。
    """
    path = themes_file(config)
    if path is None or not path.is_file():
        return {}
    mtime = path.stat().st_mtime
    cached = _places.get(path)
    if cached is not None and cached[0] == mtime:
        return dict(cached[1])
    found: dict[str, Path] = {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("%s を読めません: %s", path, e)
        data = {}
    for name, value in data.items():
        try:
            found[theme_name(str(name))] = check_place(config, value)
        except PlaceError as e:
            log.warning("%s の %s は使いません: %s", path, name, e)
    _places[path] = (mtime, found)
    return dict(found)


def check_places(config: Config) -> None:
    """themes.toml を、設定を読むときに確かめる（書き間違いを黙って既定の場所に戻さない）。"""
    path = themes_file(config)
    if path is None or not path.is_file():
        return
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise PlaceError(f"{path} を読めません: {e}") from None
    for name, value in data.items():
        if not _SAFE_NAME.match(theme_name(str(name))):
            raise PlaceError(f"{path} の {name}: テーマの名前（チャンネル名）として使えません")
        try:
            check_place(config, value)
        except PlaceError as e:
            raise PlaceError(f"{path} の {name}: {e}") from None


def _inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    root = root.expanduser().resolve()
    return path == root or root in path.parents


def check_place(config: Config, value: object) -> Path:
    """テーマに使ってよいフォルダか確かめて、その場所（絶対パス）を返す。使えなければ PlaceError。"""
    if not isinstance(value, str) or not value.strip():
        raise PlaceError("フォルダの場所を文字で書いてください（例: \"~/src/my-repo\"）")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise PlaceError("フォルダの場所は ~ か / から書いてください")
    path = path.resolve()
    if not path.is_dir():
        raise PlaceError(f"フォルダがありません: {path}")
    home = Path.home().resolve()
    if path in (home, Path("/")):
        raise PlaceError("ホームやディスクの一番上は、テーマに使えません")
    for root, what in ((config.research_root, "研究テーマの既定の置き場所"), (config.repo_root, "Kei Agent 自身のフォルダ"),
                       (config.state_dir, "Kei Agent の状態の置き場所"), (config.user_dir, "Kei Agent の設定の置き場所"),
                       (config.secrets_dir, "秘密情報の置き場所")):
        if _inside(path, root):
            raise PlaceError(f"{what}の中は、テーマに使えません")
    for name in PROTECTED_FOLDERS:
        if _inside(path, home / name) and not config.allow_protected_folders:
            raise PlaceError(f"{name} は macOS の保護フォルダなので、そのままでは使えません（使うなら config.toml に "
                             "allow_protected_folders = true と、フルディスクアクセスの許可が要ります。deploy/README.md）")
    return path


def icloud_warning(path: Path) -> str:
    """iCloud Drive で同期している場所なら、その注意。そうでなければ空文字。"""
    if _inside(path.resolve(), ICLOUD_DRIVE):
        return "このフォルダは iCloud Drive で同期しています。同期の途中でファイルが消えたように見えることがあるので、注意してね"
    return ""


def save_place(config: Config, name: str, folder: Path) -> None:
    """themes.toml に「テーマの名前 = フォルダ」を書く（同じテーマがあれば置き換える。ほかの行はそのまま）。"""
    path = themes_file(config)
    if path is None:
        raise PlaceError("利用者のフォルダが決まっていないので、themes.toml に書けません")
    current: dict[str, str] = {}
    if path.is_file():
        current = {str(k): str(v) for k, v in tomllib.loads(path.read_text(encoding="utf-8")).items()}
    current[theme_name(name)] = str(folder)

    def quoted(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["# 研究テーマと置き場所（テーマの名前 = フォルダ）。書いていないテーマは research_root/<テーマ>",
             "# Slack で「既存のフォルダを使う」を選ぶと、Kei Agent がここに書き足す。手で直してもよい", ""]
    lines += [f"{quoted(key)} = {quoted(value)}" for key, value in sorted(current.items())]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _places.pop(path, None)
