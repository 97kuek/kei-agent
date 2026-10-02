"""使ってよいモデルの一覧と、その確かめ（モジュールの用途・担当の表で固定したモデル）。

モデル名を書くのは、ここと module.toml の [use_cases] だけ。モジュールの定義と担当の表（agents.csv）を読むときに
確かめるので、モジュールの枠に置く。用途ごとにどのモデルで動かすかを決めるのは execution.model_policy。
"""

from __future__ import annotations

from kei_agent.framework.modules import ModuleSpec

PROVIDERS = frozenset({"codex", "claude"})

ALLOWED_MODELS = {
    "codex": frozenset({"gpt-6-luna", "gpt-6-sol", "gpt-6-astra"}),
    "claude": frozenset({"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5", "claude-fable-5"}),
}
# 依頼者が明示したときだけ使うモデル（用途の manual = true でしか書けない）
MANUAL_ONLY_MODELS = {"codex": frozenset({"gpt-6-astra"}), "claude": frozenset({"claude-fable-5"})}
# 担当の表（agents.csv）で書ける effort。空は CLI の既定（Claude では考えない）
ALLOWED_EFFORTS = {
    "codex": frozenset({"", "minimal", "low", "medium", "high", "xhigh"}),
    "claude": frozenset({"", "low", "medium", "high", "xhigh", "max"}),
}
# コアの用途（execution.model_policy.UseCase）。モジュールの用途は、これと同じ名前にできない
CORE_USE_CASES = ("routing",)


class ModelCatalogError(ValueError):
    """モジュールの用途のモデルが、コアの一覧の外にある（設定を読むときに ConfigError にする）。"""


def check_module_recipes(spec: ModuleSpec) -> None:
    """モジュールの用途は、コアの用途と名前がぶつからず、モデルはコアの一覧の中にあること（設定を読むときに確かめる）。"""
    for use_case in spec.actor.use_cases if spec.actor else ():
        if use_case.name in CORE_USE_CASES:
            raise ModelCatalogError(f"モジュール「{spec.name}」の用途 {use_case.name} は、コアの用途と同じ名前です")
        for provider, (model, effort) in use_case.recipes.items():
            if effort not in ALLOWED_EFFORTS[provider]:
                raise ModelCatalogError(f"モジュール「{spec.name}」の用途 {use_case.name} の {provider} の effort {effort} は使えません"
                                        f"（使えるのは {', '.join(sorted(ALLOWED_EFFORTS[provider] - {''}))} か空）")
            if not is_allowed_model(provider, model):
                raise ModelCatalogError(f"モジュール「{spec.name}」の用途 {use_case.name} の {provider} のモデル {model} は使えません"
                                  f"（使えるのは {', '.join(sorted(ALLOWED_MODELS[provider]))}）")
            if model in MANUAL_ONLY_MODELS.get(provider, ()) and not use_case.manual:
                raise ModelCatalogError(f"モジュール「{spec.name}」の用途 {use_case.name} の {model} は、依頼者が明示したときだけの用途"
                                  "（manual = true）でしか使えません")


# 担当の表（agents.csv）で固定したモデル。actor → (provider, model, effort)。load_config が入れ直す
_PINS: dict[str, tuple[str, str, str]] = {}


def pin_error(actor: str, provider: str, model: str, effort: str) -> str:
    """表で固定するモデルの書き間違い（無ければ空）。"""
    if not model:
        return "effort を書くときは model も書いてください" if effort else ""
    if provider not in PROVIDERS:
        return "model を書くときは engine も書いてください"
    if model in MANUAL_ONLY_MODELS.get(provider, ()):
        return f"{model} は依頼者が明示したときだけのモデルなので、固定できません"
    if not is_allowed_model(provider, model):
        return f"{provider} では {model} を使えません（使えるのは {', '.join(sorted(ALLOWED_MODELS[provider] - MANUAL_ONLY_MODELS[provider]))}）"
    if effort not in ALLOWED_EFFORTS[provider]:
        return f"{provider} の effort は {', '.join(sorted(ALLOWED_EFFORTS[provider] - {''}))} か空にしてください: {effort}"
    return ""


def pin_models(profiles: dict) -> None:
    """表で固定したモデルを覚える（AgentProfile の model が空でないもの）。前のものは捨てる。"""
    _PINS.clear()
    _PINS.update({actor: (p.provider, p.model, p.effort) for actor, p in profiles.items() if p.model})


def pinned(actor: str, provider: str) -> tuple[str, str] | None:
    """その actor を provider で動かすときに固定したモデル (model, effort)。表の engine と違う provider なら None
    （App Home で一時的に切り替えたときは、module.toml の用途ごとの選び分けに戻る）。"""
    pin = _PINS.get(actor)
    return pin[1:] if pin is not None and pin[0] == provider else None


def is_allowed_model(provider: str, model: str) -> bool:
    return model in ALLOWED_MODELS.get(provider, ())
