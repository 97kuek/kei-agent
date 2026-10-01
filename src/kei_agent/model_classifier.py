"""自由文を、選択済み provider の軽量 recipe で用途分類する。"""

from __future__ import annotations

from kei_agent import modules, runner
from kei_agent.configuration.config import Config
from kei_agent.model_json import json_object
from kei_agent.model_policy import ModelPolicyError, UseCase, resolve_classifier, use_case_of
from kei_agent.router import workspace


class UsageLimited(RuntimeError):
    """分類に使った provider の quota が尽きた。別 recipe で再試行してはいけない。"""

    def __init__(self, reset_at: float):
        self.reset_at = reset_at
        super().__init__("軽量分類器の利用上限に達しました")


def parse(text: str, allowed: frozenset[UseCase | str]) -> UseCase | str | None:
    """形式不正・低信頼は None。呼び出し側が通常 recipe へ安全に戻す。"""
    try:
        data = json_object(text, "use_case")
    except ValueError:
        return None
    try:
        confidence = float(data.get("confidence"))
        use_case = use_case_of(str(data.get("use_case") or ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return use_case if confidence >= 0.8 and use_case in allowed else None


async def classify(config: Config, store, actor: str, prompt: str, *,
                   provider: str | None = None) -> UseCase | str:
    """担当の用途を分類する（どの担当も同じ呼び方）。モジュールの実行役は、module.toml に classify
    （見分け方）があれば Web を使う用途の中から選び、無ければ分類器を動かさずに default_use_case にする。"""
    spec = modules.known().get(actor)
    if spec is None or spec.actor is None:
        raise KeyError(actor)
    return await classify_module(config, store, spec, prompt, provider=provider)


async def classify_module(config: Config, store, spec: modules.ModuleSpec, prompt: str, *,
                          provider: str | None = None) -> str:
    """モジュールの実行役の用途。classify（見分け方）が無ければ、分類器を動かさずに default_use_case。"""
    assert spec.actor is not None
    if not spec.actor.classify:
        return spec.actor.default_use_case
    # 手動指定だけの用途（manual）は、分類器には選ばせない
    cases = [u.name for u in spec.actor.use_cases if not u.offline and not u.manual]
    return str(await _classify(config, store, spec.name, prompt, frozenset(cases), spec.actor.default_use_case,
                               ", ".join(cases), spec.actor.classify, provider=provider))


async def _classify(config: Config, store, actor: str, prompt: str, allowed: frozenset[UseCase | str],
                    fallback: UseCase | str, candidates: str, guidance: str, *,
                    provider: str | None = None) -> UseCase | str:
    try:
        recipe = resolve_classifier(config, store, actor, provider=provider)
    except ModelPolicyError:
        return fallback
    classifier_prompt = ("次の依頼をユースケースに分類してください。JSON 1行だけで答えてください。"
                         "形: {\"use_case\":\"候補名\",\"confidence\":0.0から1.0}。\n"
                         f"候補は {candidates}。\n{guidance}\n"
                         f"迷うときは {fallback} と confidence を 0.7 未満にしてください。\n\n依頼:\n{prompt[:1200]}")
    try:
        result = await runner.run_model(
            config, runner.ExecutionRequest(workspace(config, actor), recipe, None, "", "", read_only=True),
            classifier_prompt,
        )
    except Exception:
        # 分類器の障害で利用者の依頼自体を落とさない。再試行・昇格はしない。
        return fallback
    if result.limit_reset_at is not None:
        raise UsageLimited(result.limit_reset_at)
    return parse(result.text, allowed) or fallback
