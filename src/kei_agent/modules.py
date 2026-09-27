"""モジュール（機能のまとまり）の定義 `module.toml` を読む（docs/extensibility.md）。

組み込みのモジュールはリポジトリ直下の `modules/<名前>/`、利用者のモジュールは `~/.config/kei-agent/modules/<名前>/`。
どちらも同じ形で読む。変わらない事実（module.toml）は設定を読むときに確かめ、動きは使うときに読み込む
（本体側の module.py は本体の起動のときに load_code で、担当プロセスの agent.py は共通の起動コマンドが load_agent で）。

コアのほかの部品（設定・モデル・制限の表）がここを読むので、ここからは kei_agent のどこも読み込まない
（module.py が読み込むのは窓口の kei_agent.api、agent.py は kei_agent_a2a.api だけ）。
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
import tomllib
import types
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

# [channels] に書くと、ほかのどれにも当たらないチャンネル（研究テーマ）を受け持つ名前。受け持てるのは1つのモジュールだけ
ALL_CHANNELS = "*"
# 本体のチャンネルのうち、モジュールが会話を受け持てるもの（core_channels に書く。名前は config.toml の [channels]）。
# improve は Kei Agent のチャンネル（#00_kei-agent）。困りごとの知らせは、受け持つモジュールが無くても本体が出す
CORE_CHANNELS = ("improve",)
# この Kei Agent が読める枠の版。枠（module.toml の形と core の窓口）を変えるときに上げる
API_VERSION = 1
SPEC_FILE = "module.toml"
CODE_FILE = "module.py"
AGENT_FILE = "agent.py"
# A2A ではない常駐のプロセス（[process] kind = "service"）。serve(config, port) を置く
SERVICE_FILE = "service.py"
# 手で動かすコマンド（setup など）。COMMANDS = {"名前": main(argv)} を置く
COMMANDS_FILE = "commands.py"
# モジュールの投稿のボタンと入力の画面の名前の頭（本体が、どのモジュールのものかを見分ける）
ACTION_PREFIX = "kei_agent_module:"
# モジュールのフォルダを、この名前の下のパッケージとして読み込む（module.py から同じフォルダのファイルを読めるように）
PACKAGE = "kei_agent_modules"
BUILTIN_DIR = Path(__file__).resolve().parents[2] / "modules"
PROVIDERS = ("claude", "codex")
ACCESS = ("none", "read", "write")
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_USE_CASE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_TOP_KEYS = {"api", "name", "label", "description", "depends", "actor", "use_cases", "process", "channels",
             "core_channels", "schedules", "settings", "slash_commands"}
_DEPENDS_KEYS = {"requires", "optional"}
_ACTOR_KEYS = {"prompt", "plugin", "files", "shell", "web", "notion", "timeout_minutes", "default_use_case",
               "classify", "connectors", "workspace"}
# 実行役の作業場に最初に置く CLAUDE.md のひな形（モジュールのフォルダにあれば使う）
WORKSPACE_TEMPLATE = "CLAUDE.template.md"
_CONNECTOR_KEYS = {"name", "claude_server", "claude_tools", "codex_apps"}
_CODEX_APP_KEYS = {"name", "namespace", "tools"}
# skill と二の柵のフック（Claude Code の plugin）の置き場所。モジュールのフォルダの中
PLUGIN_DIR = "plugin"
_USE_CASE_KEYS = {"offline", "manual", *PROVIDERS}
_RECIPE_KEYS = {"model", "effort"}
_PROCESS_KEYS = {"port", "kind"}
# 常駐のプロセスの種類。a2a は担当（agent.py の SKILLS と Executor）、service はそれ以外の口（service.py の serve）
PROCESS_KINDS = ("a2a", "service")
_SCHEDULE_KEYS = {"label", "short", "default"}
# Slack のスラッシュコマンドの名前（/ は付けない）
_SLASH = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# 設定（[settings]）の既定の値に使える形。利用者の設定（config.toml の [<名前>]）は、既定と同じ形にする
SETTING_TYPES = (str, int, float, bool, list, dict)


class ModuleError(ValueError):
    """module.toml が読めない、形が違う、枠の版が合わない、ほかのモジュールとぶつかる。"""


@dataclass(frozen=True)
class UseCaseSpec:
    name: str
    # Web を使わない回（外の文を材料として渡す回。docs/architecture.md の「知識」）
    offline: bool
    # provider → (model, effort)。model が使ってよいものかは、コアのモデルの一覧（model_policy）で確かめる
    recipes: dict[str, tuple[str, str]]
    # 依頼者が依頼の頭に [[名前]] と書いたときだけ使う用途（分類器は選ばない。いちばん強いモデルなど）
    manual: bool = False


@dataclass(frozen=True)
class CodexAppSpec:
    """Codex の App（表示名）と、使う道具（`<namespace>.<道具>`）。"""
    name: str
    namespace: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class ConnectorSpec:
    """アカウントの連携（Claude は claude.ai のコネクタ、Codex は App）。書いた道具だけを使える（読む道具だけを書く）。"""
    name: str
    claude_server: str
    claude_tools: tuple[str, ...]
    codex_apps: tuple[CodexAppSpec, ...] = ()


@dataclass(frozen=True)
class ActorSpec:
    """AI の実行役。provider は App Home で選び、どこまで触れるかはここに書いたものが制限の表の行になる。"""
    prompt: str
    plugin: bool
    files: str
    shell: bool
    web: bool
    notion: str
    timeout_minutes: int | None
    # 自由な質問の用途。classify を書かなければ、分類器を動かさずにこれを使う
    default_use_case: str
    use_cases: tuple[UseCaseSpec, ...]
    # 自由な質問の用途を、軽いモデルで選び分けるときの見分け方（Web を使う用途の中から。迷えば default_use_case）
    classify: str = ""
    connectors: tuple[ConnectorSpec, ...] = ()
    # 作業場（~ から書ける）。無ければ状態の置き場の agents/<名前>
    workspace: str = ""


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    label: str
    short: str
    default: str


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    label: str
    description: str
    path: Path
    builtin: bool
    requires: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    actor: ActorSpec | None = None
    # 常駐のプロセスの番地。持たないモジュールは None
    port: int | None = None
    # その常駐のプロセスが A2A の担当ではない（service.py の serve で動く。Notion のゲートウェイなど）
    service: bool = False
    # チャンネルの種類 → 既定の名前（番号を外した名前。設定の [channels] で変えられる）
    channels: dict[str, tuple[str, ...]] = field(default_factory=dict)
    schedules: tuple[ScheduleSpec, ...] = ()
    # 設定の名前 → 既定の値（config.toml の [<名前>] で変えられる）
    settings: dict[str, object] = field(default_factory=dict)
    # Slack のスラッシュコマンド（/ を付けない名前 → 説明）。Slack の App にも同じ名前で足す
    slash_commands: dict[str, str] = field(default_factory=dict)
    # 会話を受け持つ本体のチャンネル（CORE_CHANNELS の中から）
    core_channels: tuple[str, ...] = ()

    @property
    def catch_all(self) -> bool:
        """ほかのどれにも当たらないチャンネル（研究テーマ）を受け持つか（[channels] に "*"）。"""
        return any(names == (ALL_CHANNELS,) for names in self.channels.values())


def _check_keys(data: dict, known: set[str], where: str) -> None:
    unknown = sorted(set(data) - known)
    if unknown:
        raise ModuleError(f"{where} に知らないキーがあります: {', '.join(unknown)}（使えるキー: {', '.join(sorted(known))}）")


def _table(data: dict, key: str, where: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ModuleError(f"{where} の {key} はテーブルにしてください")
    return value


def _names(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ModuleError(f"{where} は文字列の配列にしてください")
    return tuple(value)


def _use_cases(data: dict, where: str) -> tuple[UseCaseSpec, ...]:
    found = []
    for name, spec in data.items():
        at = f"{where} の [use_cases.{name}]"
        if not _USE_CASE.match(name) or not isinstance(spec, dict):
            raise ModuleError(f"{at}: 用途の名前は英小文字と _ で、中身はテーブルにしてください")
        _check_keys(spec, _USE_CASE_KEYS, at)
        recipes = {}
        for provider in PROVIDERS:
            recipe = spec.get(provider)
            if recipe is None:
                continue
            if not isinstance(recipe, dict) or not isinstance(recipe.get("model"), str):
                raise ModuleError(f"{at} の {provider} は {{ model = \"…\", effort = \"…\" }} の形にしてください")
            _check_keys(recipe, _RECIPE_KEYS, f"{at} の {provider}")
            recipes[provider] = (recipe["model"], str(recipe.get("effort", "")))
        if not recipes:
            raise ModuleError(f"{at}: claude か codex の、少なくとも片方のモデルを書いてください")
        found.append(UseCaseSpec(name, bool(spec.get("offline", False)), recipes, bool(spec.get("manual", False))))
    return tuple(found)


def _tools(value: object, where: str) -> tuple[str, ...]:
    tools = _names(value, where)
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", tool) for tool in tools):
        raise ModuleError(f"{where} の道具の名前は、英数字と _ . - だけにしてください")
    return tools


def _connectors(value: object, at: str) -> tuple[ConnectorSpec, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ModuleError(f"{at} の connectors は [[actor.connectors]] の形にしてください")
    found = []
    for item in value:
        where = f"{at} の connectors の {item.get('name') or '（名前なし）'}"
        _check_keys(item, _CONNECTOR_KEYS, where)
        apps = []
        for app in item.get("codex_apps", []):
            if not isinstance(app, dict):
                raise ModuleError(f"{where} の codex_apps は [[actor.connectors.codex_apps]] の形にしてください")
            _check_keys(app, _CODEX_APP_KEYS, f"{where} の codex_apps")
            if not app.get("name") or not app.get("namespace"):
                raise ModuleError(f"{where} の codex_apps には name（App の表示名）と namespace が要ります")
            apps.append(CodexAppSpec(str(app["name"]), str(app["namespace"]),
                                     _tools(app.get("tools", []), f"{where} の codex_apps")))
        server = str(item.get("claude_server") or "")
        claude_tools = _tools(item.get("claude_tools", []), where)
        if not item.get("name") or not ((server and claude_tools) or apps):
            raise ModuleError(f"{where}: name と、claude_server と claude_tools か codex_apps の、少なくとも片方が要ります")
        found.append(ConnectorSpec(str(item["name"]), server, claude_tools, tuple(apps)))
    return tuple(found)


def _actor(data: dict, use_cases: tuple[UseCaseSpec, ...], where: str) -> ActorSpec:
    at = f"{where} の [actor]"
    _check_keys(data, _ACTOR_KEYS, at)
    for key in ("files", "notion"):
        if data.get(key, "none") not in ACCESS:
            raise ModuleError(f"{at} の {key} は {' / '.join(ACCESS)} のどれかにしてください")
    if not use_cases:
        raise ModuleError(f"{at}: 実行役には、少なくとも1つの [use_cases.<名前>] が要ります")
    default = str(data.get("default_use_case") or next((u.name for u in use_cases if not u.manual), ""))
    if default not in {u.name for u in use_cases if not u.manual}:
        raise ModuleError(f"{at} の default_use_case（{default}）が、[use_cases] の手動指定でない用途にありません")
    timeout = data.get("timeout_minutes")
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        raise ModuleError(f"{at} の timeout_minutes は正の整数にしてください")
    prompt = str(data.get("prompt") or "")
    if not prompt.endswith(".md"):
        raise ModuleError(f"{at} の prompt は指示書のファイル名（例: knowledge.md）にしてください")
    if not (Path(where).parent / prompt).is_file():
        raise ModuleError(f"{at} の prompt（{prompt}）が、module.toml と同じフォルダにありません")
    plugin = bool(data.get("plugin", False))
    if plugin and not (Path(where).parent / PLUGIN_DIR / ".claude-plugin" / "plugin.json").is_file():
        raise ModuleError(f"{at} の plugin = true には、同じフォルダに {PLUGIN_DIR}/.claude-plugin/plugin.json が要ります")
    classify = str(data.get("classify") or "")
    if classify and len([u for u in use_cases if not u.offline and not u.manual]) < 2:
        raise ModuleError(f"{at} の classify は、Web を使う用途（offline でないもの）が2つ以上あるときに書いてください")
    return ActorSpec(prompt=prompt, plugin=plugin, files=data.get("files", "none"),
                     shell=bool(data.get("shell", False)), web=bool(data.get("web", False)),
                     notion=data.get("notion", "none"), timeout_minutes=timeout, default_use_case=default,
                     use_cases=use_cases, classify=classify,
                     connectors=_connectors(data.get("connectors", []), at),
                     workspace=str(data.get("workspace") or ""))


def _schedules(data: dict, where: str) -> tuple[ScheduleSpec, ...]:
    found = []
    for name, spec in data.items():
        at = f"{where} の [schedules.{name}]"
        if not _USE_CASE.match(name) or not isinstance(spec, dict):
            raise ModuleError(f"{at}: 定期処理の名前は英小文字と _ で、中身はテーブルにしてください")
        _check_keys(spec, _SCHEDULE_KEYS, at)
        default = str(spec.get("default", ""))
        if default and not _HHMM.match(default):
            raise ModuleError(f"{at} の default は HH:MM か、空文字（既定では動かさない）にしてください")
        label = str(spec.get("label") or name)
        found.append(ScheduleSpec(name, label, str(spec.get("short") or label), default))
    return tuple(found)


def _settings(data: dict, where: str) -> dict[str, object]:
    """[settings] の名前と既定の値。値の形（文字・数・真偽・配列・表）が、利用者の設定で書ける形になる。"""
    for name, value in data.items():
        if not _USE_CASE.match(name):
            raise ModuleError(f"{where} の [settings] {name}: 設定の名前は英小文字・数字・_ にしてください")
        if not isinstance(value, SETTING_TYPES):
            raise ModuleError(f"{where} の [settings] {name}: 既定の値は文字・数・真偽・配列・表のどれかにしてください")
    return dict(data)


def load_spec(directory: Path, builtin: bool = False) -> ModuleSpec:
    """1つのモジュールの定義を読んで確かめる。"""
    path = directory / SPEC_FILE
    where = str(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ModuleError(f"{where} を読めません: {e}") from None
    _check_keys(data, _TOP_KEYS, where)
    if data.get("api") != API_VERSION:
        raise ModuleError(f"{where}: 枠の版が合いません（このモジュールは api = {data.get('api')!r}、"
                          f"この Kei Agent は api = {API_VERSION}）。CHANGELOG の直し方を見てください")
    name = str(data.get("name") or "")
    if not _NAME.match(name) or name != directory.name:
        raise ModuleError(f"{where}: name は英小文字・数字・- で、フォルダの名前（{directory.name}）と同じにしてください")
    depends = _table(data, "depends", where)
    _check_keys(depends, _DEPENDS_KEYS, f"{where} の [depends]")
    use_cases = _use_cases(_table(data, "use_cases", where), where)
    actor = _actor(data["actor"], use_cases, where) if "actor" in data else None
    if use_cases and actor is None:
        raise ModuleError(f"{where}: [use_cases] は [actor]（AI の実行役）といっしょに書いてください")
    process = _table(data, "process", where)
    _check_keys(process, _PROCESS_KEYS, f"{where} の [process]")
    port = process.get("port")
    if process and (not isinstance(port, int) or not 1024 <= port <= 65535):
        raise ModuleError(f"{where} の [process] port は 1024〜65535 の整数にしてください")
    kind = process.get("kind", "a2a")
    if kind not in PROCESS_KINDS:
        raise ModuleError(f"{where} の [process] kind は {' / '.join(PROCESS_KINDS)} のどれかにしてください")
    if process and kind == "a2a" and not (directory / AGENT_FILE).is_file():
        raise ModuleError(f"{where}: [process] で動かす {AGENT_FILE}（SKILLS と class Executor）が、同じフォルダにありません")
    if process and kind == "service" and not (directory / SERVICE_FILE).is_file():
        raise ModuleError(f"{where}: [process] kind = \"service\" で動かす {SERVICE_FILE}（serve(config, port)）が、"
                          "同じフォルダにありません")
    channels = {kind: _names(names, f"{where} の [channels] {kind}")
                for kind, names in _table(data, "channels", where).items()}
    for kind, names in channels.items():
        if ALL_CHANNELS in names and names != (ALL_CHANNELS,):
            raise ModuleError(f"{where} の [channels] {kind}: \"{ALL_CHANNELS}\"（ほかのどれにも当たらないチャンネル）は、"
                              "それだけを書いてください")
    core_channels = _names(data.get("core_channels", []), f"{where} の core_channels")
    unknown = sorted(set(core_channels) - set(CORE_CHANNELS))
    if unknown:
        raise ModuleError(f"{where} の core_channels に、受け持てない本体のチャンネルがあります: {', '.join(unknown)}"
                          f"（受け持てるもの: {', '.join(CORE_CHANNELS)}）")
    schedules = _schedules(_table(data, "schedules", where), where)
    slash = _table(data, "slash_commands", where)
    for command, text in slash.items():
        if not _SLASH.match(command) or not isinstance(text, str):
            raise ModuleError(f"{where} の [slash_commands] {command}: 名前は英小文字・数字・_・-（/ は付けない）で、"
                              "値は説明の文字にしてください")
    if (channels or core_channels or schedules or slash) and not (directory / CODE_FILE).is_file():
        raise ModuleError(f"{where}: [channels]・core_channels・[schedules]・[slash_commands] を動かす {CODE_FILE}"
                          "（class Module）が、同じフォルダにありません")
    return ModuleSpec(
        name=name, label=str(data.get("label") or name), description=str(data.get("description") or ""),
        path=directory, builtin=builtin,
        requires=_names(depends.get("requires", []), f"{where} の requires"),
        optional=_names(depends.get("optional", []), f"{where} の optional"),
        actor=actor, port=port, service=bool(process) and kind == "service", channels=channels, schedules=schedules,
        settings=_settings(_table(data, "settings", where), where), slash_commands=dict(slash),
        core_channels=core_channels)


def discover(directory: Path, builtin: bool = False) -> dict[str, ModuleSpec]:
    """そのフォルダの下の、module.toml を持つフォルダを全部読む。"""
    if not directory.is_dir():
        return {}
    return {child.name: load_spec(child, builtin) for child in sorted(directory.iterdir())
            if (child / SPEC_FILE).is_file()}


@cache
def builtin() -> dict[str, ModuleSpec]:
    """組み込みのモジュール（リポジトリ直下の modules/）。"""
    return discover(BUILTIN_DIR, builtin=True)


_user: dict[str, ModuleSpec] = {}


def register_user_modules(directory: Path) -> dict[str, ModuleSpec]:
    """利用者のモジュール（~/.config/kei-agent/modules/）を読んで、知っているモジュールに足す。設定を読むときに呼ぶ。

    設定がリポジトリ直下にあるとき（例の設定を読むときなど）は、そこの modules/ は組み込みなので読まない。
    """
    found = {} if directory.resolve() == BUILTIN_DIR.resolve() else discover(directory)
    for name, spec in found.items():
        if name in builtin():
            raise ModuleError(f"{spec.path}: 組み込みのモジュール「{name}」と同じ名前です。別の名前にしてください")
    _user.clear()
    _user.update(found)
    _check_collisions(known())
    return found


def known() -> dict[str, ModuleSpec]:
    """知っているモジュール（組み込みと、読んだ利用者のもの）。オンになっているかは設定の modules で決まる。"""
    return {**builtin(), **_user}


def _check_collisions(specs: dict[str, ModuleSpec]) -> None:
    """用途・定期処理・チャンネルの種類・番地は、モジュールどうしでぶつからないこと。"""
    seen: dict[str, str] = {}
    for spec in specs.values():
        keys = [f"用途「{u.name}」" for u in (spec.actor.use_cases if spec.actor else ())]
        keys += [f"定期処理「{s.name}」" for s in spec.schedules]
        keys += [f"チャンネルの種類「{kind}」" for kind in spec.channels]
        keys += [f"スラッシュコマンド「/{name}」" for name in spec.slash_commands]
        keys += [f"番地「{spec.port}」"] if spec.port else []
        for key in keys:
            if key in seen and seen[key] != spec.name:
                raise ModuleError(f"モジュール「{seen[key]}」と「{spec.name}」の{key}がぶつかっています")
            seen[key] = spec.name


def enabled(names) -> list[ModuleSpec]:
    """設定の modules の順に、知っているモジュールの定義を並べる（知らない名前は、設定を読むときに断ってある）。"""
    specs = known()
    return [specs[name] for name in names if name in specs]


def action_id(module: str, name: str) -> str:
    """モジュールの投稿のボタンや入力の画面の名前（本体が、押されたらそのモジュールの on_action / on_view に渡す）。"""
    return f"{ACTION_PREFIX}{module}:{name}"


def schedule_owner(names, schedule: str) -> ModuleSpec | None:
    """その定期処理を持つ、オンのモジュール（names は設定の modules）。本体の定期処理なら None。"""
    return next((spec for spec in enabled(names) if any(s.name == schedule for s in spec.schedules)), None)


def use_case_owner(use_case: str) -> ModuleSpec | None:
    """その用途を持つモジュール。コアの用途なら None。"""
    for spec in known().values():
        if spec.actor and any(u.name == use_case for u in spec.actor.use_cases):
            return spec
    return None


def package(spec: ModuleSpec) -> str:
    """モジュールのフォルダを Python のパッケージとして読めるようにして、その名前を返す。

    module.py は `from . import texts` のように、同じフォルダのファイルを読み込める。
    """
    name = f"{PACKAGE}.{spec.name.replace('-', '_')}"
    if PACKAGE not in sys.modules:
        parent = types.ModuleType(PACKAGE)
        parent.__path__ = []
        sys.modules[PACKAGE] = parent
    if getattr(sys.modules.get(name), "__path__", None) != [str(spec.path)]:
        # 同じ名前のモジュールを別の場所から読み直すとき（試験など）は、前に読んだものを捨てる
        for loaded in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
            del sys.modules[loaded]
        found = types.ModuleType(name)
        found.__path__ = [str(spec.path)]
        sys.modules[name] = found
    return name


def load_code(spec: ModuleSpec) -> type | None:
    """そのモジュールの動き（module.py の class Module）を読み込む。module.py が無ければ None。

    module.toml に書いたのに動かす口が無いもの（定期処理の run_schedule、チャンネルの on_message）は、
    起動のときに断る（黙って動かないままにしない）。
    """
    where = spec.path / CODE_FILE
    if not where.is_file():
        return None
    importlib.invalidate_caches()
    code = importlib.import_module(f"{package(spec)}.module")
    cls = getattr(code, "Module", None)
    if not isinstance(cls, type):
        raise ModuleError(f"{where} に class Module がありません")
    if spec.schedules and not callable(getattr(cls, "run_schedule", None)):
        raise ModuleError(f"{where}: [schedules] があるので、class Module に run_schedule(name, day) を書いてください")
    agenda = getattr(cls, "agenda", None)
    if callable(agenda):
        try:
            inspect.signature(agenda).bind(None, 7, None)
        except TypeError:
            raise ModuleError(f"{where}: agenda は agenda(self, days, kinds=None) の形にしてください") from None
    if spec.slash_commands and not callable(getattr(cls, "on_slash_command", None)):
        raise ModuleError(f"{where}: [slash_commands] があるので、class Module に on_slash_command(name, body) を書いてください")
    for hook, args, shape in (("on_event", (None, "", {}), "on_event(self, kind, data)"),
                              ("home", (None,), "home(self)"),
                              ("on_home_action", (None, "", {}), "on_home_action(self, name, action)"),
                              ("on_slash_command", (None, "", {}), "on_slash_command(self, name, body)"),
                              ("on_action", (None, "", {}), "on_action(self, name, body)"),
                              ("on_view", (None, "", {}), "on_view(self, name, body)"),
                              ("material", (None, 0.0), "material(self, now)"),
                              ("on_start", (None,), "on_start(self)")):
        found = getattr(cls, hook, None)
        if found is None:
            continue
        try:
            inspect.signature(found).bind(*args)
        except (TypeError, ValueError):
            raise ModuleError(f"{where}: {hook} は {shape} の形にしてください") from None
    on_message = getattr(cls, "on_message", None)
    if (spec.channels or spec.core_channels) and not callable(on_message):
        raise ModuleError(f"{where}: [channels] か core_channels があるので、class Module に on_message(req, skill, params)"
                          " を書いてください")
    if callable(on_message):
        try:
            inspect.signature(on_message).bind(None, None, skill="", params={})
        except TypeError:
            raise ModuleError(f"{where}: on_message は on_message(self, req, skill=\"\", params=None) の形にしてください"
                              "（研究全体のチャンネルで振り分け係が選んだ仕事が、skill と params で届く）") from None
    return cls


def load_agent(spec: ModuleSpec):
    """担当プロセスの動き（agent.py の SKILLS と class Executor）を読み込む。共通の起動コマンドが使う。

    `async background(executor)` があれば、担当と同じプロセスで動かし続ける仕事（声ならマイクの会話）。
    """
    where = spec.path / AGENT_FILE
    importlib.invalidate_caches()
    code = importlib.import_module(f"{package(spec)}.agent")
    if not isinstance(getattr(code, "SKILLS", None), (list, tuple)) or not isinstance(getattr(code, "Executor", None),
                                                                                       type):
        raise ModuleError(f"{where} に SKILLS（名刺に載せる仕事の一覧）と class Executor を書いてください")
    background = getattr(code, "background", None)
    if background is not None:
        try:
            inspect.signature(background).bind(None)
            ok = inspect.iscoroutinefunction(background)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ModuleError(f"{where}: background は async def background(executor) の形にしてください")
    return code


def load_service(spec: ModuleSpec):
    """A2A ではない常駐のプロセス（service.py の serve(config, port)）を読み込む。共通の起動コマンドが使う。"""
    where = spec.path / SERVICE_FILE
    importlib.invalidate_caches()
    code = importlib.import_module(f"{package(spec)}.service")
    serve = getattr(code, "serve", None)
    try:
        inspect.signature(serve).bind(None, 0)
    except (TypeError, ValueError):
        raise ModuleError(f"{where} に serve(config, port) を書いてください") from None
    return code


def load_commands(spec: ModuleSpec) -> dict:
    """そのモジュールの、手で動かすコマンド（commands.py の COMMANDS）。無ければ空。

    `kei-agent-module <名前> <コマンド> [引数...]` で動く。値は main(argv: list[str]) の関数。
    """
    if not (spec.path / COMMANDS_FILE).is_file():
        return {}
    importlib.invalidate_caches()
    code = importlib.import_module(f"{package(spec)}.commands")
    commands = getattr(code, "COMMANDS", None)
    if not isinstance(commands, dict) or not all(isinstance(k, str) and callable(v) for k, v in commands.items()):
        raise ModuleError(f"{spec.path / COMMANDS_FILE} に COMMANDS = {{\"名前\": main}} を書いてください")
    return commands
