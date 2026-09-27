"""用途ごとの model / effort を決める。

provider の選択は agent ごとに保持する。ここは選択された provider に対して、
用途ごとの固定 recipe を返すだけであり、別 provider や上位 model への fallback はしない。
コアの用途は下の表、モジュールの用途は module.toml の [use_cases] から引く。使ってよいモデルの一覧は、ここにだけ置く。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from kei_agent import modules
from kei_agent.config import AGENT_PLUGINS, ConfigError, model_actors

PROVIDERS = frozenset({"codex", "claude"})

ALLOWED_MODELS = {
    "codex": frozenset({"gpt-6-luna", "gpt-6-sol", "gpt-6-astra"}),
    "claude": frozenset({"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5", "claude-fable-5"}),
}
# 依頼者が明示したときだけ使うモデル（用途の manual = true でしか書けない）
MANUAL_ONLY_MODELS = {"codex": frozenset({"gpt-6-astra"}), "claude": frozenset({"claude-fable-5"})}
# 依頼の頭で用途を指定する書き方（[[research-design]]。名前の _ は - で書く）
_EXPLICIT = re.compile(r"^\s*\[\[([a-z][a-z0-9-]{0,39})\]\]\s*", re.IGNORECASE)


class ModelPolicyError(ValueError):
    """選択された provider で安全に実行できない。"""


class UseCase(StrEnum):
    ROUTING = "routing"
    OVERVIEW_DAILY = "overview_daily"
    OVERVIEW_PLAN = "overview_plan"
    SELF_FIX_DESIGN = "self_fix_design"
    SELF_FIX_IMPLEMENTATION = "self_fix_implementation"
    SELF_FIX_REVIEW = "self_fix_review"


@dataclass(frozen=True)
class ResolvedModel:
    actor: str
    # コアの用途は UseCase、モジュールの用途は名前の文字列（UseCase も文字列として比べられる）
    use_case: UseCase | str
    provider: str
    model: str
    reasoning_effort: str
    manual_only: bool = False


# `(model, effort)`: empty Claude effort deliberately means non-thinking.
_RECIPES: dict[tuple[str, UseCase], tuple[str, str]] = {
    ("codex", UseCase.ROUTING): ("gpt-6-luna", "low"),
    ("claude", UseCase.ROUTING): ("claude-haiku-4-5", ""),
    ("codex", UseCase.OVERVIEW_DAILY): ("gpt-6-luna", "medium"),
    ("claude", UseCase.OVERVIEW_DAILY): ("claude-sonnet-5", "medium"),
    ("codex", UseCase.OVERVIEW_PLAN): ("gpt-6-sol", "high"),
    ("claude", UseCase.OVERVIEW_PLAN): ("claude-opus-5", "high"),
    ("codex", UseCase.SELF_FIX_DESIGN): ("gpt-6-sol", "xhigh"),
    ("claude", UseCase.SELF_FIX_DESIGN): ("claude-opus-5", "high"),
    ("codex", UseCase.SELF_FIX_IMPLEMENTATION): ("gpt-6-sol", "high"),
    ("claude", UseCase.SELF_FIX_IMPLEMENTATION): ("claude-sonnet-5", "high"),
    ("codex", UseCase.SELF_FIX_REVIEW): ("gpt-6-sol", "medium"),
    ("claude", UseCase.SELF_FIX_REVIEW): ("claude-sonnet-5", "high"),
}

_ACTOR_USE_CASES = {
    "router": frozenset({UseCase.ROUTING, UseCase.OVERVIEW_DAILY, UseCase.OVERVIEW_PLAN}),
    "self_fix": frozenset({
        UseCase.SELF_FIX_DESIGN, UseCase.SELF_FIX_IMPLEMENTATION, UseCase.SELF_FIX_REVIEW,
    }),
}


def allowed_use_cases(actor: str) -> frozenset[UseCase | str]:
    """actor が通常経路または手動例外で使える use case。モジュールの実行役は module.toml の [use_cases]。"""
    if actor in _ACTOR_USE_CASES:
        return _ACTOR_USE_CASES[actor]
    spec = modules.known().get(actor)
    if spec is None or spec.actor is None:
        raise ModelPolicyError(f"未知の actor です: {actor}")
    return frozenset(u.name for u in spec.actor.use_cases)


def use_case_of(value: UseCase | str) -> UseCase | str:
    """用途の名前を、コアの用途（UseCase）か、モジュールの用途（名前の文字列）にする。知らなければ ModelPolicyError。"""
    try:
        return UseCase(value)
    except ValueError:
        if modules.use_case_owner(str(value)) is not None:
            return str(value)
        raise ModelPolicyError(f"未知の use case です: {value}") from None


def _recipe(provider: str, use_case: UseCase | str) -> tuple[str, str] | None:
    """(model, effort)。コアの表か、その用途を持つモジュールの module.toml から。"""
    if (provider, use_case) in _RECIPES:
        return _RECIPES[(provider, use_case)]
    owner = modules.use_case_owner(str(use_case))
    if owner is None or owner.actor is None:
        return None
    spec = next(u for u in owner.actor.use_cases if u.name == use_case)
    return spec.recipes.get(provider)


def check_module_recipes(spec: modules.ModuleSpec) -> None:
    """モジュールの用途は、コアの用途と名前がぶつからず、モデルはコアの一覧の中にあること（設定を読むときに確かめる）。"""
    for use_case in spec.actor.use_cases if spec.actor else ():
        if use_case.name in UseCase.__members__.values():
            raise ConfigError(f"モジュール「{spec.name}」の用途 {use_case.name} は、コアの用途と同じ名前です")
        for provider, (model, _effort) in use_case.recipes.items():
            if not is_allowed_model(provider, model):
                raise ConfigError(f"モジュール「{spec.name}」の用途 {use_case.name} の {provider} のモデル {model} は使えません"
                                  f"（使えるのは {', '.join(sorted(ALLOWED_MODELS[provider]))}）")
            if model in MANUAL_ONLY_MODELS.get(provider, ()) and not use_case.manual:
                raise ConfigError(f"モジュール「{spec.name}」の用途 {use_case.name} の {model} は、依頼者が明示したときだけの用途"
                                  "（manual = true）でしか使えません")


def is_allowed_model(provider: str, model: str) -> bool:
    return model in ALLOWED_MODELS.get(provider, ())


def is_manual(use_case: UseCase | str) -> bool:
    """依頼者が明示したときだけ使う用途か（モジュールの manual = true）。"""
    owner = modules.use_case_owner(str(use_case))
    return bool(owner and owner.actor and any(u.name == use_case and u.manual for u in owner.actor.use_cases))


def explicit_use_case(actor: str, text: str) -> tuple[UseCase | str | None, str]:
    """依頼の頭の [[名前]] で指定された、その担当の用途と、指定を外した文。指定が無いか、知らない名前なら (None, text)。

    名前の _ は - で書く（[[research-design]] は research_design）。手動指定だけの用途（manual）も選べる。
    """
    match = _EXPLICIT.match(text or "")
    if not match:
        return None, text
    name = match.group(1).lower().replace("-", "_")
    try:
        case = use_case_of(name)
        allowed = allowed_use_cases(actor)
    except ModelPolicyError:
        return None, text
    return (case, text[match.end():].strip()) if case in allowed else (None, text)


def resolve(actor: str, provider: str, use_case: UseCase | str, *, manual: bool = False) -> ResolvedModel:
    """選択済み provider の recipe だけを返す。"""
    if actor not in model_actors():
        raise ModelPolicyError(f"未知の actor です: {actor}")
    if provider not in PROVIDERS:
        raise ModelPolicyError(f"provider を選んでください: {actor}")
    case = use_case_of(use_case)
    if case not in allowed_use_cases(actor):
        raise ModelPolicyError(f"{actor} では {case} を使えません")
    found = _recipe(provider, case)
    if found is None:
        raise ModelPolicyError(f"{actor} では {case} を {provider} で使えません")
    model, effort = found
    if not is_allowed_model(provider, model):
        raise ModelPolicyError(f"許可されていない model です: {model}")
    if is_manual(case):
        # 手動指定だけの用途（module.toml の manual = true）
        if not manual:
            raise ModelPolicyError(f"{case} は依頼者による手動指定だけで使えます")
        return ResolvedModel(actor, case, provider, model, effort, manual_only=True)
    return ResolvedModel(actor, case, provider, model, effort)


def resolve_selected(config, store, actor: str, use_case: UseCase | str, *, manual: bool = False) -> ResolvedModel:
    """App Home で選択された provider から recipe を解決する。"""
    # settings は Config を import するため、循環 import を避けて遅延 import にする。
    from kei_agent.settings import selected_provider

    return resolve(actor, selected_provider(config, store, actor), use_case, manual=manual)


def classifies(actor: str) -> bool:
    """自由な質問の用途を、軽量分類で選び分ける担当か（本体の plugin actor と、module.toml に classify を書いた実行役）。"""
    if actor in AGENT_PLUGINS:
        return True
    spec = modules.known().get(actor)
    return spec is not None and spec.actor is not None and bool(spec.actor.classify)


def resolve_classifier(config, store, actor: str, *, provider: str | None = None) -> ResolvedModel:
    """軽量分類だけに使う routing recipe。

    通常の ``resolve`` は actor 固有の仕事だけを許可する。分類は例外的に routing
    recipe を使うが、実行 actor は依頼の担当のままにして runner の権限境界を保つ。
    """
    if not classifies(actor):
        raise ModelPolicyError(f"{actor} は軽量分類を使えません")
    from kei_agent.settings import selected_provider

    provider = provider or selected_provider(config, store, actor)
    if provider not in PROVIDERS:
        raise ModelPolicyError(f"provider を選んでください: {actor}")
    try:
        model, effort = _RECIPES[(provider, UseCase.ROUTING)]
    except KeyError as e:
        raise ModelPolicyError(f"{provider} の分類 recipe がありません") from e
    if not is_allowed_model(provider, model):
        raise ModelPolicyError(f"許可されていない model です: {model}")
    return ResolvedModel(actor, UseCase.ROUTING, provider, model, effort)


def validate_resolved(recipe: ResolvedModel) -> None:
    """runner に渡る recipe が policy から作られた値と完全一致するか確認する。

    ``ResolvedModel`` は dataclass なので、呼び出し元が直接作ること自体は Python では防げない。
    CLI 起動直前に再解決して比べることで、allowlist 内の手動例外を通常用途へ偽装する経路を閉じる。
    plugin actor の ``routing`` だけは分類器専用の軽量例外として同じ固定値を検証する。
    """
    if recipe.use_case is UseCase.ROUTING and classifies(recipe.actor):
        expected_values = _RECIPES.get((recipe.provider, UseCase.ROUTING))
        expected = (ResolvedModel(recipe.actor, UseCase.ROUTING, recipe.provider, *expected_values)
                    if expected_values is not None else None)
    else:
        try:
            expected = resolve(recipe.actor, recipe.provider, recipe.use_case, manual=recipe.manual_only)
        except ModelPolicyError as e:
            raise ModelPolicyError(f"許可されない recipe です: {e}") from e
    if expected != recipe:
        raise ModelPolicyError(f"許可されない recipe です: {recipe.actor}/{recipe.use_case}")
