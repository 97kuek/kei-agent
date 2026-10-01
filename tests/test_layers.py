"""コアの領域の依存の向き（docs/architecture.md の「コードの地図」）。下の層から上の層を読み込まない。

モジュールの枠・設定 ← 記録 ← 作業場 ← AI の実行 ← 会話 ← 予定 ← 窓口（kei_agent.api） ← 運用

同じ層の中は読み合ってよい。型の注釈のためだけの読み込み（TYPE_CHECKING）は数えない。関数の中の読み込みは数える。
"""

import ast
from pathlib import Path

from kei_agent.configuration.config import REPO_ROOT

CORE = REPO_ROOT / "src" / "kei_agent"
LAYERS = {
    "framework": 0,
    "configuration": 1,
    "storage": 2,
    "workspaces": 3,
    "execution": 4,
    "conversation": 5,
    "scheduling": 6,
    "api": 7,
    "operations": 8,
}
# 向きを破ってよい読み込みと、その理由（増やすときは、ほどけないかを先に考える）
ALLOWED = {
    # 会話の本体が、モジュールを迎え入れるために窓口（Core）を作る。窓口はすべての領域を使うので、いちばん上にある
    ("conversation/assistant.py", "api"),
}


def _layer_of(path: Path) -> str:
    rel = path.relative_to(CORE)
    return "api" if rel == Path("api.py") else rel.parts[0]


def _type_checking_lines(tree: ast.AST) -> set[int]:
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            for child in node.body:
                lines.update(range(child.lineno, (child.end_lineno or child.lineno) + 1))
    return lines


def _imported_layers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _type_checking_lines(tree)
    found = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in skip:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] != "kei_agent":
                continue
            if len(parts) > 1:
                found.add(parts[1])
            else:
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "kei_agent" and len(parts) > 1:
                    found.add(parts[1])
    return {name for name in found if name in LAYERS}


def test_every_core_file_belongs_to_a_layer():
    """領域のフォルダの外に、コアのファイルを置かない（窓口の api.py と、テストの道具 testing/ は別）。"""
    stray = [p.name for p in CORE.glob("*.py") if p.name not in ("__init__.py", "api.py")]
    folders = {p.name for p in CORE.iterdir() if p.is_dir() and p.name not in ("__pycache__", "testing")}
    assert stray == [] and folders == set(LAYERS) - {"api"}


def test_no_layer_reads_a_layer_above_it():
    upward = []
    for path in sorted(CORE.rglob("*.py")):
        if "testing" in path.relative_to(CORE).parts or path.name == "__init__.py" and path.parent == CORE:
            continue
        mine = _layer_of(path)
        for other in _imported_layers(path):
            rel = str(path.relative_to(CORE))
            if other != mine and LAYERS[other] > LAYERS[mine] and (rel, other) not in ALLOWED:
                upward.append(f"{rel} -> {other}")
    assert upward == [], "下の層から上の層を読み込んでいる（docs/architecture.md の「コードの地図」）"
