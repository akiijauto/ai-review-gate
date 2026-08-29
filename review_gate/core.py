"""マルチレビュアー品質ゲートの中核。

設計の要点:
- 1つの万能プロンプトではなく、担当軸を絞った専門レビュアーを並列に走らせる。
  1レビュアーに全部見せると評価が総花的になり、点数が甘くなることが
  実運用で分かったため（詳細はREADME）。
- 執筆モデルとレビューモデルは別にする。同じモデルに書かせて採点させると
  自分の癖を減点できない。
- リスクティアが high の文書は、点数が閾値を超えても自動合格させない。
  ゲートは運用の心がけではなくコード側に実装する。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMClient


@dataclass(frozen=True)
class ReviewerSpec:
    """1レビュアーの定義。担当軸（axes）の点数だけを返す契約。"""
    name: str
    prompt_path: Path
    axes: dict[str, int]  # 軸名 -> 配点上限


@dataclass
class GateResult:
    score: int
    max_score: int
    breakdown: dict[str, int]
    issues: list[str]
    passed: bool
    auto_publish_allowed: bool
    per_reviewer: dict[str, dict] = field(default_factory=dict)


def _clamp(value, upper: int) -> int:
    """モデルが配点上限を超えた点や数値でない値を返しても集計を壊さない。"""
    try:
        return max(0, min(int(value), upper))
    except (TypeError, ValueError):
        return 0


def run_reviewer(client: LLMClient, spec: ReviewerSpec, document: str) -> dict:
    prompt = spec.prompt_path.read_text(encoding="utf-8").format(document=document)
    return client.generate_json(prompt)


def review(
    client: LLMClient,
    reviewers: list[ReviewerSpec],
    document: str,
    *,
    pass_threshold: int,
    risk_tier: str = "normal",
    max_workers: int = 3,
) -> GateResult:
    """専門レビュアーを並列実行し、集計してゲート判定する。

    risk_tier:
        "normal" — 閾値以上で合格＝自動公開可
        "high"   — 閾値以上でも auto_publish_allowed は False のまま。
                   公開は必ず人間の承認操作を経る（下書き保存で停止する運用を想定）
    """
    if risk_tier not in ("normal", "high"):
        raise ValueError(f"未知のrisk_tier: {risk_tier}")

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_reviewer, client, spec, document): spec
            for spec in reviewers
        }
        for future in as_completed(futures):
            spec = futures[future]
            results[spec.name] = future.result()

    breakdown: dict[str, int] = {}
    issues: list[str] = []
    for spec in reviewers:
        result = results[spec.name]
        for axis, upper in spec.axes.items():
            breakdown[axis] = _clamp(result.get(axis, 0), upper)
        found = result.get("issues", [])
        if isinstance(found, list):
            issues.extend(str(x) for x in found)

    score = sum(breakdown.values())
    max_score = sum(upper for spec in reviewers for upper in spec.axes.values())
    passed = score >= pass_threshold

    return GateResult(
        score=score,
        max_score=max_score,
        breakdown=breakdown,
        issues=issues,
        passed=passed,
        # high は合格しても自動公開させない。この行がゲートの本体。
        auto_publish_allowed=passed and risk_tier == "normal",
        per_reviewer=results,
    )


def load_reviewers(config: dict, base_dir: Path) -> list[ReviewerSpec]:
    """config.yaml の reviewers 定義を ReviewerSpec に変換する。"""
    specs = []
    for entry in config["reviewers"]:
        specs.append(
            ReviewerSpec(
                name=entry["name"],
                prompt_path=base_dir / entry["prompt"],
                axes={a["name"]: int(a["max"]) for a in entry["axes"]},
            )
        )
    return specs


def format_report(result: GateResult) -> str:
    status = "合格" if result.passed else "要修正"
    lines = [
        f"総合: {result.score}/{result.max_score}点 ({status})",
        f"自動公開: {'可' if result.auto_publish_allowed else '不可（人間の承認が必要）'}",
        "内訳: " + " ".join(f"{k}:{v}" for k, v in result.breakdown.items()),
    ]
    if result.issues:
        lines.append("指摘:")
        lines.extend(f"  - {issue}" for issue in result.issues)
    return "\n".join(lines)


def save_result(result: GateResult, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "score": result.score,
        "max_score": result.max_score,
        "breakdown": result.breakdown,
        "issues": result.issues,
        "passed": result.passed,
        "auto_publish_allowed": result.auto_publish_allowed,
        "per_reviewer": result.per_reviewer,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
