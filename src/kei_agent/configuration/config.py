"""設定の読み込み。

秘密情報（Slackのトークンなど）は環境変数から、それ以外は config.toml から読む。
config.toml は利用者のフォルダ（既定は ~/.config/kei-agent/。環境変数 KEI_AGENT_HOME で変えられる）に置き、
リポジトリには例（config.example.toml）だけを置く。プロフィールと指示書の差し替えも、同じフォルダから読む
（docs/extensibility.md）。
"""

from __future__ import annotations

import copy
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from kei_agent.framework import modules

REPO_ROOT = Path(__file__).resolve().parents[3]
# 利用者のもの（設定・プロフィール・指示書の差し替え・秘密情報）を置く場所
DEFAULT_HOME = "~/.config/kei-agent"
CONFIG_FILE = "config.toml"
PROFILE_FILE = "profile.md"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.toml"

# 決まった時刻の処理の時刻。空文字は「その処理を行わない」（settings.schedule_time と同じ形）
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# skill を持つエージェント（`plugin/<agent>/`）。声やルーターには skill を渡さない
# 本体の担当で skill を持つもの（研究はモジュールになったので、今は無い）
AGENT_PLUGINS: frozenset[str] = frozenset()
# 本体が持つ実行役。router は Daily/Retro の横断的な計画も担う。モジュールの実行役は module.toml の [actor] から足す
CORE_ACTORS = AGENT_PLUGINS | frozenset({"router"})


def model_actors() -> frozenset[str]:
    """provider を選ぶ実行役。コアのものと、知っているモジュール（組み込みと利用者のもの）のもの。"""
    return CORE_ACTORS | frozenset(name for name, spec in modules.known().items() if spec.actor)


@dataclass(frozen=True)
class AgentProfile:
    """agent が使う provider。どこまで触れるか（道具・連携）は制限の表（agent_policy.py）が決める。

    skill は手順、profile は実行器を決める。skill の中にモデル名を埋め込まないため、
    Claude と Codex を同じ agent から切り替えられる。
    """

    # provider は担当の表（agents.csv）の engine。空文字は「まだ選んでいない」。
    provider: str = ""
    # 担当の表（agents.csv）で固定したモデル。空なら module.toml の用途ごとの選び分け（model_policy.resolve）
    model: str = ""
    effort: str = ""
    # 担当の表で決めたアカウントのフォルダ（CLAUDE_CONFIG_DIR・CODEX_HOME）。空ならプロセスの既定
    claude_account: str = ""
    codex_account: str = ""


def _default_agent_profiles() -> dict[str, AgentProfile]:
    return {agent: AgentProfile() for agent in model_actors()}


def _default_modules() -> tuple[str, ...]:
    """設定に modules を書かなければ、組み込みのモジュールを全部使う。"""
    return tuple(modules.builtin())


def _default_module_channels() -> dict[str, tuple[str, ...]]:
    return {kind: names for spec in modules.builtin().values() for kind, names in spec.channels.items()}


def _default_module_times() -> dict[str, str]:
    return {s.name: s.default for spec in modules.builtin().values() for s in spec.schedules}


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def path_without_venv(path: str, repo_root: Path) -> str:
    """PATH から Kei Agent 自身の `.venv/bin` を外す。

    `uv run` が PATH の先頭に足すので、そのまま渡すと、テーマの中で `python3` と打ったときに
    研究用ではなく Kei Agent の Python が当たってしまう。
    """
    venv_bin = str(repo_root / ".venv" / "bin")
    return os.pathsep.join(p for p in path.split(os.pathsep) if p and p != venv_bin)




@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool = True
    # "HH:MM"（ローカル時刻）。空文字にするとその処理を行わない
    daily: str = "08:00"
    review: str = "21:00"
    night: str = "00:00"
    # Mac のスリープなどで逃した処理を、何時間後まで実行するか
    catch_up_hours: float = 3
    night_max_tasks: int = 5
    stall_days: int = 3
    unanswered_hours: int = 24
    # モジュールの定期処理の時刻（名前 → HH:MM。書かなければ module.toml の既定）
    module_times: dict[str, str] = field(default_factory=_default_module_times)


@dataclass(frozen=True)
class MaintenanceConfig:
    enabled: bool = True
    # 毎晩の保守（古いファイルの整理とバックアップ）を行う時刻。振り返りのあとにする
    time: str = "22:00"
    # ~/research を Git でコミットして push する（deploy/backup-init.sh で準備する）
    backup: bool = True
    # テーマのディレクトリで動かした Claude のセッションの記録を残す日数
    session_retention_days: int = 90
    # スレッドのログ（.kei-agent/threads/*.md）を残す日数
    thread_log_retention_days: int = 180


def notion_id(value: str) -> str:
    """Notion の ID を比べられる形にする（ハイフンを外して小文字）。"""
    return str(value or "").replace("-", "").strip().lower()


# ゲートウェイの利用者のうち、本体（と手で動かす setup）。共通ホームと、書いてあるすべてのホームに届く
MAIN_CLIENT = "kei-agent"


@dataclass(frozen=True)
class NotionConfig:
    """Notion のホームのページ ID（agents.csv の notion 列）。ゲートウェイはこの下だけを通す。"""
    # 共通ホーム（本体だけ）
    hub_home: str = ""
    # 研究ホーム（kei-agent-notion-setup の notion.json と同じ）
    research_home: str = ""
    # 授業ホーム（kei-agent-module course setup の notion-course.json と同じ）
    course_home: str = ""
    # そのほかのモジュールのホーム（agents.csv のその行の notion。モジュールの名前 → ページ ID）
    homes: dict[str, str] = field(default_factory=dict)

    def client_homes(self) -> dict[str, str]:
        """ゲートウェイの利用者（本体のほか）と、それぞれが届くホーム。書いてあるものだけ。

        利用者の名前は、モジュール（と研究）の名前。その担当の AI も Python も、この名前の合言葉で呼ぶ。
        """
        found = {"research": self.research_home, "course": self.course_home, **self.homes}
        return {name: home for name, home in found.items() if home}


def _notion(data: dict) -> NotionConfig:
    _check_keys(data, {f.name for f in fields(NotionConfig)}, "[notion]")
    homes = data.get("homes", {})
    if not isinstance(homes, dict) or not all(isinstance(v, str) for v in homes.values()):
        raise ConfigError('agents.csv の notion 列には、ページ ID を書いてください')
    allowed = set(modules.known()) | {"research"}
    unknown = sorted(set(homes) - allowed)
    if unknown:
        raise ConfigError(f"agents.csv の notion 列に知らないモジュールがあります: {', '.join(unknown)}"
                          f"（書けるもの: {', '.join(sorted(allowed))}）")
    for name, key in (("research", "research_home"), ("course", "course_home")):
        if data.get(key) and homes.get(name):
            raise ConfigError(f"agents.csv の notion 列で、{name} のホームが2か所にあります")
    values = {key: notion_id(str(value)) for key, value in data.items() if key != "homes"}
    return NotionConfig(**values, homes={name: notion_id(home) for name, home in homes.items()})


@dataclass(frozen=True)
class A2AConfig:
    """ほかのエージェントの住所（docs/architecture.md の「振り分けと A2A」）。

    `[a2a.agents]` に「名前 = 住所」を並べる。名前は launchd・ログ・秘密情報ファイル・ポートの
    呼び名と同じにする。書かなければ、そのエージェントは使わない（研究を書かなければ本体の中で動かす）。
    """
    agents: dict[str, str] = field(default_factory=dict)
    # 相手を待つ時間（秒）。AI を動かす仕事には、AI の上限時間を足して待つ
    timeout_seconds: float = 300
    # 本体の A2A の口（声のレイヤからの問い合わせを受ける）。担当を呼べるのは本体だけ
    orchestrator: str = ""

    def url(self, name: str) -> str:
        return self.agents.get(name, "")


@dataclass(frozen=True)
class Config:
    # 研究テーマだけを置く場所（研究エージェントの領分）
    research_root: Path
    # Kei Agent 自身のもの（研究全体の作業場と、状態の書き出し）。研究テーマと混ぜない
    agent_root: Path
    # 授業の作業場（大学エージェントの claude が動くところ。資料は置かない）
    course_root: Path
    state_dir: Path
    repo_root: Path
    allowed_user_id: str
    overview_channels: tuple[str, ...] = ("overview", "research-overview")
    # `#0-kei-agent`。先頭の番号は外して照合する（themes.theme_name）
    improve_channels: tuple[str, ...] = ("kei-agent",)
    # 大学エージェントに取り次ぐチャンネル（claude -p は動かさない）
    # 仕事エージェントに取り次ぐチャンネル
    # 使うモジュール（設定の modules）と、そのチャンネル（種類 → 番号を外した名前）
    modules: tuple[str, ...] = field(default_factory=_default_modules)
    module_channels: dict[str, tuple[str, ...]] = field(default_factory=_default_module_channels)
    max_concurrent_runs: int = 2
    run_timeout_minutes: int = 30
    job_poll_seconds: int = 60
    job_parallel: int = 1
    agent_profiles: dict[str, AgentProfile] = field(default_factory=_default_agent_profiles)
    # 依頼者の依頼がこの回数たまったスレッドでは、新しいスレッドに区切るボタンを出す。0 なら出さない
    handoff_after_turns: int = 8
    allowed_domains: tuple[str, ...] = ()
    allow_write: tuple[Path, ...] = ()
    # config.toml の [sandbox] deny_read（書かなければ None）。実際に読ませない場所は柵が決める（guard.denied_reads）
    deny_read: tuple[Path, ...] | None = None
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    pueue_bin: str = "pueue"
    schedule: ScheduleConfig = field(default_factory=lambda: ScheduleConfig())
    maintenance: MaintenanceConfig = field(default_factory=lambda: MaintenanceConfig())
    a2a: A2AConfig = field(default_factory=lambda: A2AConfig())
    notion: NotionConfig = field(default_factory=lambda: NotionConfig())
    # エージェント同士の合言葉（環境変数 KEI_AGENT_A2A_TOKEN）
    a2a_token: str = ""
    # 利用者のフォルダ（~/.config/kei-agent/）。None なら、プロフィールも指示書の差し替えも使わない（テスト）
    user_dir: Path | None = None
    # 秘密情報の置き場所（[paths] secrets。既定は利用者のフォルダの secrets/）。いつも AI に読ませない
    secrets_dir: Path | None = None
    # モジュールの設定のうち、config.toml の [<名前>] に書いたもの（書かなかったものは settings() が既定で埋める）
    module_settings: dict[str, dict] = field(default_factory=dict)
    # macOS の保護フォルダ（書類・デスクトップ・ダウンロード）を、研究テーマの置き場所に選べるか（既定は選べない）
    allow_protected_folders: bool = False
    # 担当の表（agents.csv）を読んだときの場所。None なら表が無い（組み込み全部・AI は未選択）
    agents_table: Path | None = None
    # 表の folder 列で決めた、研究と大学のほかの担当の作業場（モジュールの名前 → フォルダ）
    module_folders: dict[str, Path] = field(default_factory=dict)

    def settings(self, name: str) -> dict:
        """そのモジュールの設定（module.toml の [settings] の既定に、config.toml の [<名前>] を重ねたもの）。

        返すのは写しなので、書き換えても設定には響かない。
        """
        spec = modules.known().get(name)
        defaults = spec.settings if spec is not None else {}
        return copy.deepcopy({**defaults, **self.module_settings.get(name, {})})

    def module_workspace(self, name: str) -> Path:
        """モジュールの実行役の作業場。担当の表の folder 列、無ければ module.toml の [actor] workspace（それも無ければ
        状態の置き場の agents/<名前>）。大学は course_root（表の course の行の folder。書かなければ ~/course）。
        """
        if name == "course":
            return self.course_root
        if name in self.module_folders:
            return self.module_folders[name]
        spec = modules.known().get(name)
        workspace = spec.actor.workspace if spec is not None and spec.actor is not None else ""
        return _expand(workspace) if workspace else self.state_dir / "agents" / name

    def module_state(self, name: str) -> Path:
        """モジュールが持つファイルの置き場（状態の置き場の modules/<名前>。作業用のフォルダなど）。"""
        return self.state_dir / "modules" / name

    def prompt_file(self, name: str, module: str = "") -> Path:
        """指示書。利用者のフォルダの prompts/ に同じ名前のファイルがあれば、そちらを使う（丸ごと差し替え）。

        module を渡すと、そのモジュールの指示書（modules/<名前>/ の中）を探す。コアの指示書はリポジトリの prompts/。
        """
        if self.user_dir is not None and (own := self.user_dir / "prompts" / name).is_file():
            return own
        spec = modules.known().get(module) if module else None
        return (spec.path if spec is not None else self.repo_root / "prompts") / name

    @property
    def profile_text(self) -> str:
        """利用者のプロフィール（話し方、所属、興味など）。会話する担当の指示書に差し込む。無ければ空。

        先頭の題（`# プロフィール`）と `<!-- -->` のコメント（書き方の説明）は外す。
        """
        path = self.user_dir / PROFILE_FILE if self.user_dir is not None else None
        if path is None or not path.is_file():
            return ""
        text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        return re.sub(r"\A\s*# [^\n]*\n", "", text).strip()

    @property
    def db_path(self) -> Path:
        return self.state_dir / "kei-agent.db"

    @property
    def hub_state_path(self) -> Path:
        """共通ホームの ID 控え。研究ホームの notion.json とは独立させる。"""
        return self.state_dir / "hub.json"

    def agent_plugin_dir(self, agent: str) -> Path:
        """そのエージェントの skill の置き場（モジュールのフォルダの `plugin/`）。

        `--plugin-dir` に plugin の親を渡すと、Claude Code は中の plugin を全部読む。
        担当外の skill を同じ claude に見せないため、必ずエージェント1つぶんを名指しする。
        """
        spec = modules.known().get(agent)
        if spec is not None and spec.actor is not None and spec.actor.plugin:
            # モジュールの skill とフックは、そのモジュールのフォルダの plugin/
            return spec.path / modules.PLUGIN_DIR
        if agent not in AGENT_PLUGINS:
            raise ValueError(f"未知のagent: {agent}（使えるのは {', '.join(sorted(AGENT_PLUGINS))}）")
        return self.repo_root / "plugin" / agent

    @property
    def notion_gateway_url(self) -> str:
        """Notion ゲートウェイの MCP の口（Notion のモジュール modules/notion/ の常駐のプロセス）。"""
        spec = modules.known().get("notion")
        return f"http://127.0.0.1:{spec.port if spec is not None and spec.port else 8791}/mcp"

    @property
    def notion_gateway_api(self) -> str:
        """同じゲートウェイの、決まった処理用の Notion API の口（`/notion/v1`）。"""
        return gateway_endpoint(self.notion_gateway_url, "notion/v1")

    @property
    def overview_dir(self) -> Path:
        """研究全体・中長期の方針のチャンネルが書く場所。

        Daily・振り返り・声の記録は、研究だけでなく授業と仕事の内容も含むので、
        研究テーマの隣ではなく Kei Agent 側に置く（docs/architecture.md）。
        """
        return self.agent_root / "overview"


def gateway_endpoint(mcp_url: str, path: str) -> str:
    """Notion gateway の MCP の URL（…/mcp）から、同じサーバーの別の口を作る。"""
    return f"{mcp_url.rstrip('/').removesuffix('/mcp')}/{path.lstrip('/')}"


class ConfigError(ValueError):
    pass


# 書き間違いが黙って無視されないよう、使えるキーをすべて書き出しておく
TOP_LEVEL_KEYS = {
    "agent_root", "state_dir", "max_concurrent_runs", "run_timeout_minutes",
    "job_poll_seconds", "job_parallel", "handoff_after_turns", "sandbox",
    "schedule", "maintenance", "a2a", "notion", "paths", "allow_protected_folders",
}
PATHS_KEYS = {"secrets"}
# 担当の表（agents.csv）の1行のうち、AI の列
AGENT_PROFILE_KEYS = {"provider", "model", "effort", "claude_account", "codex_account"}
# 設定に書かなかったときの置き場所。テストは conftest で一時フォルダに差し替え、本物の状態や研究データを触らない
DEFAULT_PATHS = {"research_root": "~/research", "agent_root": "~/kei-agent", "course_root": "~/course",
                 "state_dir": "~/.local/state/kei-agent"}
# 本体が持つチャンネルの種類（モジュールの種類は module.toml の [channels] から足す）
CHANNELS_KEYS = {"overview", "improve"}
# [schedule] のうち、時刻（HH:MM）を書くキー（モジュールの定期処理は module.toml の [schedules] から足す）
SCHEDULE_TIME_KEYS = ("daily", "review", "night")
SANDBOX_KEYS = {"allowed_domains", "allow_write", "deny_read"}


def _check_keys(data: dict, known: set[str], where: str) -> None:
    """知らないキーがあれば、どれが違うかを示して止める。"""
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"config.toml の {where} に知らないキーがあります: {', '.join(unknown)}"
            f"（使えるキー: {', '.join(sorted(known))}）"
        )


def _section(cls, data: dict, name: str):
    """config.toml の [name] を cls にする。"""
    _check_keys(data, {f.name for f in fields(cls)}, f"[{name}]")
    return cls(**data)


def _schedule(data: dict, module_schedules: list[modules.ScheduleSpec]) -> ScheduleConfig:
    """[schedule] を読む。モジュールの定期処理の時刻は、書かなければ module.toml の既定。"""
    names = {s.name for s in module_schedules}
    core = {k: v for k, v in data.items() if k not in names}
    _check_keys(core, {f.name for f in fields(ScheduleConfig)} - {"module_times"}, "[schedule]")
    return ScheduleConfig(**core, module_times={s.name: str(data.get(s.name, s.default)) for s in module_schedules})


def _check_times(schedule: dict, maintenance: dict, extra: tuple[str, ...] = ()) -> None:
    """決まった時刻の書き間違いを、黙って「行わない」にしない。

    `daily = "8:00"` のように書くと、時刻として読めないので処理が動かなくなる。空文字だけが
    「行わない」の意味なので、それ以外の読めない形は、起動のときに断る。extra はモジュールの定期処理の名前。
    """
    times = [(f"[schedule] {name}", schedule[name]) for name in (*SCHEDULE_TIME_KEYS, *extra) if name in schedule]
    if "time" in maintenance:
        times.append(("[maintenance] time", maintenance["time"]))
    for where, value in times:
        if value != "" and not HHMM.match(str(value)):
            raise ConfigError(
                f"config.toml の {where} は HH:MM か、空文字（行わない）にしてください: {value!r}")


def _a2a(data: dict, enabled: list[modules.ModuleSpec]) -> A2AConfig:
    """[a2a] と、その下の [a2a.agents]（名前 = 住所）を読む。担当プロセスを持つモジュールは、書かなければ
    module.toml の番地（127.0.0.1）を使う。"""
    _check_keys(data, {"agents", "timeout_seconds", "orchestrator"}, "[a2a]")
    agents = data.get("agents", {})
    if not isinstance(agents, dict) or any(not isinstance(v, str) for v in agents.values()):
        raise ConfigError("config.toml の [a2a.agents] は「名前 = \"住所\"」の形で書いてください")
    orchestrator = data.get("orchestrator", "")
    if not isinstance(orchestrator, str):
        raise ConfigError("config.toml の [a2a] orchestrator は住所の文字列で書いてください")
    # A2A ではない常駐のプロセス（Notion のゲートウェイなど）は、担当ではないので並べない
    defaults = {spec.name: f"http://127.0.0.1:{spec.port}" for spec in enabled if spec.port and not spec.service}
    return A2AConfig(agents={**defaults, **agents}, timeout_seconds=float(data.get("timeout_seconds", 300)),
                     orchestrator=orchestrator)


def _enabled_modules(data: dict, where: str) -> list[modules.ModuleSpec]:
    """担当の表でオンにしたモジュール（表が無ければ組み込み全部）を選ぶ。where は直す場所（知らせに使う）。"""
    known = modules.known()
    names = data.get("modules", list(modules.builtin()))
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ConfigError(f"{where} に知らないモジュールがあります: {', '.join(unknown)}"
                          f"（知っているもの: {', '.join(sorted(known)) or 'なし'}）")
    enabled = [known[n] for n in dict.fromkeys(names)]
    catch_all = [spec.name for spec in enabled if spec.catch_all]
    if len(catch_all) > 1:
        # ほかのどれにも当たらないチャンネル（研究テーマ）を受け持てるのは、オンのモジュールのうち1つだけ
        raise ConfigError(f"モジュール「{catch_all[0]}」と「{catch_all[1]}」が、どちらもほかのどれにも当たらないチャンネル（*）を"
                          f"受け持とうとしています。{where} でどちらかを外してください")
    for kind in modules.CORE_CHANNELS:
        owners = [spec.name for spec in enabled if kind in spec.core_channels]
        if len(owners) > 1:
            # 本体のチャンネルの会話を受け持てるのも、オンのモジュールのうち1つだけ
            raise ConfigError(f"モジュール「{owners[0]}」と「{owners[1]}」が、どちらも本体のチャンネル（{kind}）の会話を"
                              f"受け持とうとしています。{where} でどちらかを外してください")
    for schedule in modules.CORE_SCHEDULES:
        owners = [spec.name for spec in enabled if schedule in spec.core_schedules]
        if len(owners) > 1:
            raise ConfigError(f"モジュール「{owners[0]}」と「{owners[1]}」が、どちらも本体の定期処理（{schedule}）を"
                              f"受け持とうとしています。{where} でどちらかを外してください")
    for spec in enabled:
        missing = [r for r in spec.requires if r not in names]
        if missing:
            raise ConfigError(f"モジュール「{spec.name}」には {', '.join(missing)} が要ります（{where} でオンにしてください）")
    # 使ってよいモデルの一覧はコアにある（model_policy）。読み込みの順番のため、ここで読む
    from kei_agent.framework.models import ModelCatalogError, check_module_recipes
    for spec in enabled:
        try:
            check_module_recipes(spec)
        except ModelCatalogError as e:
            raise ConfigError(str(e)) from None
    return enabled


def _same_kind(value: object, default: object) -> bool:
    """利用者の設定の値が、module.toml の既定と同じ形か（小数の既定には整数も書ける。真偽は数と分ける）。"""
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(default, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


# 設定の値の形の呼び方（書き方が違うときの知らせに使う）
_KINDS = {str: "文字列", int: "整数", float: "数", bool: "true か false", list: "配列", dict: "表"}


def _module_settings(data: dict) -> dict[str, dict]:
    """モジュールの設定（config.toml の [<名前>]）を、module.toml の [settings] と見比べて読む。

    オフのモジュールの設定も確かめて残す（modules から外すたびに消さなくてよい）。
    """
    found: dict[str, dict] = {}
    for name, spec in modules.known().items():
        if name in TOP_LEVEL_KEYS or name in ("modules", "channels", "agents", "folders"):
            if spec.settings:
                raise ConfigError(f"モジュール「{name}」の設定が、config.toml の [{name}]（本体の設定）とぶつかります。"
                                  "モジュールの名前を変えてください")
            continue
        if name not in data:
            continue
        if not spec.settings:
            raise ConfigError(f"config.toml の [{name}]: モジュール「{name}」には設定がありません")
        values = data[name]
        if not isinstance(values, dict):
            raise ConfigError(f"config.toml の [{name}] はテーブルにしてください")
        _check_keys(values, set(spec.settings), f"[{name}]")
        for key, value in values.items():
            default = spec.settings[key]
            if not _same_kind(value, default):
                raise ConfigError(f"config.toml の [{name}] {key} は、{_KINDS.get(type(default), '既定と同じ形')}で書いてください")
        found[name] = dict(values)
    return found


def _agent_profiles(data: dict, where: str) -> dict[str, AgentProfile]:
    """担当の表の AI の列を読み、表に無い actor は provider 未選択にする。"""
    # 使ってよいモデルの一覧はコアにある（model_policy）。読み込みの順番のため、ここで読む
    from kei_agent.framework.models import pin_error

    unknown = sorted(set(data) - model_actors())
    if unknown:
        raise ConfigError(f"{where} に AI を使わない行があります: {', '.join(unknown)}")
    profiles = _default_agent_profiles()
    for name, raw in data.items():
        _check_keys(raw, AGENT_PROFILE_KEYS, f"{where} の {name}")
        provider, model, effort = (str(raw.get(key, "")) for key in ("provider", "model", "effort"))
        if provider not in {"", "claude", "codex"}:
            raise ConfigError(f"{where} の {name} の行: engine は claude、codex、または空にしてください")
        if error := pin_error(name, provider, model, effort):
            raise ConfigError(f"{where} の {name} の行: {error}")
        accounts = {key: str(_expand(str(raw[key]))) for key in ("claude_account", "codex_account") if raw.get(key)}
        profiles[name] = AgentProfile(provider=provider, model=model, effort=effort, **accounts)
    return profiles


def user_home(env: dict[str, str] | None = None) -> Path:
    """利用者のフォルダ（環境変数 KEI_AGENT_HOME、なければ ~/.config/kei-agent）。"""
    env = dict(os.environ) if env is None else env
    return _expand(env.get("KEI_AGENT_HOME") or DEFAULT_HOME)


def config_path(env: dict[str, str] | None = None) -> Path:
    """設定ファイルの場所（環境変数 KEI_AGENT_CONFIG、なければ利用者のフォルダの config.toml）。"""
    env = dict(os.environ) if env is None else env
    return _expand(env["KEI_AGENT_CONFIG"]) if env.get("KEI_AGENT_CONFIG") else user_home(env) / CONFIG_FILE


def config_home(env: dict[str, str] | None = None) -> Path:
    """設定を読むときの利用者のフォルダ（load_config と同じ決め方。KEI_AGENT_HOME か、KEI_AGENT_CONFIG のファイルの
    あるフォルダか、~/.config/kei-agent）。"""
    env = dict(os.environ) if env is None else env
    if env.get("KEI_AGENT_HOME") or not env.get("KEI_AGENT_CONFIG"):
        return user_home(env)
    return config_path(env).parent


def _with_table(data: dict, table: Path | None) -> dict:
    """担当の表（agents.csv）の中身を、modules・channels・agents として重ねる。表が無ければ、組み込み全部で AI は未選択。

    モジュールのオンオフ・チャンネル・AI は表だけに書く。config.toml に残っていれば、移し方を示して止める。
    """
    from kei_agent.configuration import agents_table

    old = [key for key in agents_table.REPLACED_KEYS if key in data]
    if old:
        how = "（uv run kei-agent agents init が移す）" if table is None else "（Notion のホームは notion 列に書く）"
        raise ConfigError(f"config.toml の {'・'.join(old)} は、担当の表（{agents_table.AGENTS_FILE}）に移してください{how}")
    if places := [key for key in agents_table.FOLDER_KEYS if key in data]:
        rows = "・".join(agents_table.FOLDER_KEYS[key] for key in places)
        how = "" if table is not None else "（表が無ければ uv run kei-agent agents init が移す）"
        raise ConfigError(f"config.toml の {'・'.join(places)} は、{agents_table.AGENTS_FILE} の {rows} の行の "
                          f"folder 列に移してください{how}")
    if table is None:
        return data
    try:
        return {**data, **agents_table.load(table)}
    except agents_table.TableError as e:
        raise ConfigError(str(e)) from None


def load_config(path: Path | None = None, env: dict[str, str] | None = None, *,
                agents_csv: Path | None = None) -> Config:
    """設定を読む。場所は path、環境変数 KEI_AGENT_CONFIG、利用者のフォルダの config.toml の順に探す。

    利用者のフォルダは KEI_AGENT_HOME か、設定ファイルのあるフォルダ（path を渡したとき）か、~/.config/kei-agent。
    担当の表は、利用者のフォルダの agents.csv（agents_csv で別のファイルも読める）。
    """
    env = dict(os.environ) if env is None else env
    if path is None and env.get("KEI_AGENT_CONFIG"):
        path = _expand(env["KEI_AGENT_CONFIG"])
    home = user_home(env) if env.get("KEI_AGENT_HOME") or path is None else path.parent
    path = path or home / CONFIG_FILE
    if not path.is_file():
        raise ConfigError(f"設定ファイルがありません: {path}。{EXAMPLE_CONFIG.name} を写して書き換えてください"
                          f"（例: cp {EXAMPLE_CONFIG} {path}）")
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        # 書き方の誤りも設定の誤りとして知らせる（起動・kei-agent doctor・kei-agent module が同じように扱える）
        raise ConfigError(f"{path.name} の書き方が TOML として読めません: {e}") from None

    try:
        modules.register_user_modules(home / "modules")
    except modules.ModuleError as e:
        raise ConfigError(str(e)) from None
    from kei_agent.configuration.agents_table import AGENTS_FILE

    table = agents_csv or home / AGENTS_FILE
    table = table if table.is_file() else None
    data = _with_table(data, table)
    where = table.name if table is not None else AGENTS_FILE
    schedule = data.get("schedule", {})
    channels = data.get("channels", {})
    # 担当の作業場（表の folder 列。研究はテーマのフォルダを置く場所、大学は作業場）
    folders = data.get("folders", {})
    sandbox = data.get("sandbox", {})
    enabled = _enabled_modules(data, where)
    # モジュールの設定は、そのモジュールの名前の表（[course] など）に書く
    module_settings = _module_settings(data)
    # modules・channels・agents は担当の表から来たもの（config.toml に書いたものは _with_table が断った）
    _check_keys(data, TOP_LEVEL_KEYS | {"modules", "channels", "agents", "folders"}
                | {name for name, spec in modules.known().items() if spec.settings}, "一番外側")
    # オフのモジュールのチャンネルの名前は、書いたまま残してよい（モジュールの設定の表と同じ。使うのはオンのものだけ）
    _check_keys(channels, CHANNELS_KEYS | {kind for spec in modules.known().values() for kind in spec.channels},
                "[channels]")
    _check_keys(sandbox, SANDBOX_KEYS, "[sandbox]")
    paths = data.get("paths", {})
    _check_keys(paths, PATHS_KEYS, "[paths]")
    module_schedules = [s for spec in enabled for s in spec.schedules]
    _check_times(schedule, data.get("maintenance", {}), tuple(s.name for s in module_schedules))
    state_dir = _expand(data.get("state_dir", DEFAULT_PATHS["state_dir"]))
    secrets_dir = _expand(paths.get("secrets", str(home / "secrets")))
    allow_protected = data.get("allow_protected_folders", False)
    if not isinstance(allow_protected, bool):
        raise ConfigError("config.toml の allow_protected_folders は true か false にしてください")
    config = Config(
        research_root=_expand(folders.get("research", DEFAULT_PATHS["research_root"])),
        agent_root=_expand(data.get("agent_root", DEFAULT_PATHS["agent_root"])),
        course_root=_expand(folders.get("course", DEFAULT_PATHS["course_root"])),
        module_folders={name: _expand(path) for name, path in folders.items() if name not in ("research", "course")},
        state_dir=state_dir,
        repo_root=REPO_ROOT,
        allowed_user_id=env.get("KEI_AGENT_ALLOWED_USER_ID", ""),
        overview_channels=tuple(channels.get("overview", Config.overview_channels)),
        improve_channels=tuple(channels.get("improve", Config.improve_channels)),
        modules=tuple(spec.name for spec in enabled),
        module_channels={kind: tuple(channels.get(kind, names)) for spec in enabled
                         for kind, names in spec.channels.items()},
        max_concurrent_runs=int(data.get("max_concurrent_runs", 2)),
        run_timeout_minutes=int(data.get("run_timeout_minutes", 30)),
        job_poll_seconds=int(data.get("job_poll_seconds", 60)),
        job_parallel=int(data.get("job_parallel", 1)),
        agent_profiles=_agent_profiles(data.get("agents", {}), where),
        handoff_after_turns=int(data.get("handoff_after_turns", 8)),
        allowed_domains=tuple(sandbox.get("allowed_domains", ())),
        allow_write=tuple(_expand(p) for p in sandbox.get("allow_write", ())),
        deny_read=tuple(_expand(p) for p in sandbox["deny_read"]) if "deny_read" in sandbox else None,
        claude_bin=env.get("KEI_AGENT_CLAUDE_BIN", "claude"),
        codex_bin=env.get("KEI_AGENT_CODEX_BIN", "codex"),
        pueue_bin=env.get("KEI_AGENT_PUEUE_BIN", "pueue"),
        schedule=_schedule(schedule, module_schedules),
        maintenance=_section(MaintenanceConfig, data.get("maintenance", {}), "maintenance"),
        a2a=_a2a(data.get("a2a", {}), enabled),
        notion=_notion(data.get("notion", {})),
        a2a_token=env.get("KEI_AGENT_A2A_TOKEN", ""),
        user_dir=home,
        secrets_dir=secrets_dir,
        module_settings=module_settings,
        allow_protected_folders=allow_protected,
        agents_table=table,
    )
    # 表で固定したモデルは、モデルを決めるところ（model_policy.resolve）が引く
    from kei_agent.framework.models import pin_models

    pin_models(config.agent_profiles)
    # 研究テーマの置き場所（themes.toml）の書き間違いは、起動のときに理由を出して止める
    from kei_agent.configuration.places import PlaceError, check_places

    try:
        check_places(config)
    except PlaceError as e:
        raise ConfigError(str(e)) from None
    return config
