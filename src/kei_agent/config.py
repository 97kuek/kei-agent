"""設定の読み込み。

秘密情報（Slackのトークンなど）は環境変数から、それ以外は config.toml から読む。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from kei_agent.guard import DEFAULT_DENY_READ

REPO_ROOT = Path(__file__).resolve().parents[2]

# 決まった時刻の処理の時刻。空文字は「その処理を行わない」（settings.schedule_time と同じ形）
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


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
    literature: str = "07:00"
    daily: str = "08:00"
    review: str = "21:00"
    night: str = "00:00"
    # Mac のスリープなどで逃した処理を、何時間後まで実行するか
    catch_up_hours: float = 3
    night_max_tasks: int = 5
    stall_days: int = 3
    unanswered_hours: int = 24


@dataclass(frozen=True)
class MaintenanceConfig:
    enabled: bool = True
    # 毎晩の保守（古いファイルの整理とバックアップ）を行う時刻。振り返りのあとにする
    time: str = "22:00"
    # ~/research を Git でコミットして push する（deploy/backup-init.sh で準備する）
    backup: bool = True
    # Daily と振り返りの材料のファイルを残す日数
    digest_retention_days: int = 30
    # テーマのディレクトリで動かした Claude のセッションの記録を残す日数
    session_retention_days: int = 90
    # スレッドのログ（.kei-agent/threads/*.md）を残す日数
    thread_log_retention_days: int = 180


@dataclass(frozen=True)
class A2AConfig:
    """ほかのエージェントの住所（docs/agents.md）。

    `[a2a.agents]` に「名前 = 住所」を並べる。名前は launchd・ログ・秘密情報ファイル・ポートの
    呼び名と同じにする。書かなければ、そのエージェントは使わない（研究を書かなければ本体の中で動かす）。
    """
    agents: dict[str, str] = field(default_factory=dict)
    # 相手を待つ時間（秒）。claude を動かす仕事には、claude の上限時間を足して待つ
    timeout_seconds: float = 300

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
    overview_channels: tuple[str, ...] = ("overview", "research-overview", "research-strategy")
    # `#00_kei-agent`。先頭の番号は外して照合する（themes.theme_name）
    improve_channels: tuple[str, ...] = ("kei-agent",)
    # 大学エージェントに取り次ぐチャンネル（claude -p は動かさない）
    course_channels: tuple[str, ...] = ("course",)
    # 仕事エージェントに取り次ぐチャンネル
    work_channels: tuple[str, ...] = ("work",)
    max_concurrent_runs: int = 2
    run_timeout_minutes: int = 30
    job_poll_seconds: int = 60
    job_parallel: int = 1
    model: str = ""
    # 依頼者の依頼がこの回数たまったスレッドでは、新しいスレッドに区切るボタンを出す。0 なら出さない
    handoff_after_turns: int = 8
    allowed_domains: tuple[str, ...] = ()
    allow_write: tuple[Path, ...] = ()
    deny_read: tuple[Path, ...] = ()
    claude_bin: str = "claude"
    pueue_bin: str = "pueue"
    schedule: ScheduleConfig = field(default_factory=lambda: ScheduleConfig())
    maintenance: MaintenanceConfig = field(default_factory=lambda: MaintenanceConfig())
    a2a: A2AConfig = field(default_factory=lambda: A2AConfig())
    # エージェント同士の合言葉（環境変数 KEI_AGENT_A2A_TOKEN）
    a2a_token: str = ""

    @property
    def db_path(self) -> Path:
        return self.state_dir / "kei-agent.db"

    @property
    def plugin_dir(self) -> Path:
        return self.repo_root / "plugin"

    @property
    def system_prompt_path(self) -> Path:
        return self.repo_root / "prompts" / "system.md"

    @property
    def overview_dir(self) -> Path:
        """研究全体・中長期の方針のチャンネルが書く場所。

        Daily・振り返り・声の記録は、研究だけでなく授業と仕事の内容も含むので、
        研究テーマの隣ではなく Kei Agent 側に置く（docs/design.md）。
        """
        return self.agent_root / "overview"

    @property
    def backlog_path(self) -> Path:
        # 公開しているコードのリポジトリには書かない（バックアップの対象には入る）
        return self.overview_dir / "backlog.md"


class ConfigError(ValueError):
    pass


# 書き間違いが黙って無視されないよう、使えるキーをすべて書き出しておく
TOP_LEVEL_KEYS = {
    "research_root", "agent_root", "course_root", "state_dir", "max_concurrent_runs", "run_timeout_minutes",
    "job_poll_seconds", "job_parallel", "model", "handoff_after_turns", "channels", "sandbox", "schedule",
    "maintenance", "a2a",
}
CHANNELS_KEYS = {"overview", "improve", "course", "work"}
# [schedule] のうち、時刻（HH:MM）を書くキー
SCHEDULE_TIME_KEYS = ("literature", "daily", "review", "night")
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


def _check_times(schedule: dict, maintenance: dict) -> None:
    """決まった時刻の書き間違いを、黙って「行わない」にしない。

    `daily = "8:00"` のように書くと、時刻として読めないので処理が動かなくなる。空文字だけが
    「行わない」の意味なので、それ以外の読めない形は、起動のときに断る。
    """
    times = [(f"[schedule] {name}", schedule[name]) for name in SCHEDULE_TIME_KEYS if name in schedule]
    if "time" in maintenance:
        times.append(("[maintenance] time", maintenance["time"]))
    for where, value in times:
        if value != "" and not HHMM.match(str(value)):
            raise ConfigError(
                f"config.toml の {where} は HH:MM か、空文字（行わない）にしてください: {value!r}")


def _a2a(data: dict) -> A2AConfig:
    """[a2a] と、その下の [a2a.agents]（名前 = 住所）を読む。"""
    _check_keys(data, {"agents", "timeout_seconds"}, "[a2a]")
    agents = data.get("agents", {})
    if not isinstance(agents, dict) or any(not isinstance(v, str) for v in agents.values()):
        raise ConfigError("config.toml の [a2a.agents] は「名前 = \"住所\"」の形で書いてください")
    return A2AConfig(agents=dict(agents), timeout_seconds=float(data.get("timeout_seconds", 300)))


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    path = path or Path(env.get("KEI_AGENT_CONFIG", REPO_ROOT / "config.toml"))
    data: dict = {}
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)

    schedule = data.get("schedule", {})
    channels = data.get("channels", {})
    sandbox = data.get("sandbox", {})
    _check_keys(data, TOP_LEVEL_KEYS, "一番外側")
    _check_keys(channels, CHANNELS_KEYS, "[channels]")
    _check_keys(sandbox, SANDBOX_KEYS, "[sandbox]")
    _check_times(schedule, data.get("maintenance", {}))
    return Config(
        research_root=_expand(data.get("research_root", "~/research")),
        agent_root=_expand(data.get("agent_root", "~/kei-agent")),
        course_root=_expand(data.get("course_root", "~/course")),
        state_dir=_expand(data.get("state_dir", "~/.local/state/kei-agent")),
        repo_root=REPO_ROOT,
        allowed_user_id=env.get("KEI_AGENT_ALLOWED_USER_ID", ""),
        overview_channels=tuple(channels.get("overview", Config.overview_channels)),
        improve_channels=tuple(channels.get("improve", Config.improve_channels)),
        course_channels=tuple(channels.get("course", Config.course_channels)),
        work_channels=tuple(channels.get("work", Config.work_channels)),
        max_concurrent_runs=int(data.get("max_concurrent_runs", 2)),
        run_timeout_minutes=int(data.get("run_timeout_minutes", 30)),
        job_poll_seconds=int(data.get("job_poll_seconds", 60)),
        job_parallel=int(data.get("job_parallel", 1)),
        model=data.get("model", ""),
        handoff_after_turns=int(data.get("handoff_after_turns", 8)),
        allowed_domains=tuple(sandbox.get("allowed_domains", ())),
        allow_write=tuple(_expand(p) for p in sandbox.get("allow_write", ())),
        deny_read=tuple(_expand(p) for p in sandbox.get("deny_read", DEFAULT_DENY_READ)),
        claude_bin=env.get("KEI_AGENT_CLAUDE_BIN", "claude"),
        pueue_bin=env.get("KEI_AGENT_PUEUE_BIN", "pueue"),
        schedule=_section(ScheduleConfig, schedule, "schedule"),
        maintenance=_section(MaintenanceConfig, data.get("maintenance", {}), "maintenance"),
        a2a=_a2a(data.get("a2a", {})),
        a2a_token=env.get("KEI_AGENT_A2A_TOKEN", ""),
    )
