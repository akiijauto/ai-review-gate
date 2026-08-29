"""ゲートの契約をテストで固定する。

特に重要なのは高リスクの扱い:
「high は満点でも自動公開されない」はこのシステムの安全性の本体であり、
実装の書き換えで静かに壊れてはいけないため、テストで固定する。
"""
from pathlib import Path

import pytest

from review_gate.core import ReviewerSpec, review
from review_gate.llm import MockLLMClient, extract_json


@pytest.fixture
def reviewers(tmp_path: Path) -> list[ReviewerSpec]:
    """担当軸を分けた2レビュアー。プロンプトにマーカーを埋めてモックが識別する。"""
    fact = tmp_path / "fact.md"
    fact.write_text("[事実性レビュアー]\n{document}", encoding="utf-8")
    style = tmp_path / "style.md"
    style.write_text("[文章レビュアー]\n{document}", encoding="utf-8")
    return [
        ReviewerSpec("factuality", fact, axes={"factuality": 50}),
        ReviewerSpec("style", style, axes={"clarity": 30, "structure": 20}),
    ]


def make_client(fact_score=40, clarity=25, structure=15, issues=None):
    return MockLLMClient({
        "[事実性レビュアー]": {"factuality": fact_score, "issues": issues or []},
        "[文章レビュアー]": {"clarity": clarity, "structure": structure, "issues": []},
    })


def test_合格_集計と内訳(reviewers):
    result = review(make_client(), reviewers, "本文",
                    pass_threshold=70, risk_tier="normal")
    assert result.score == 80
    assert result.max_score == 100
    assert result.breakdown == {"factuality": 40, "clarity": 25, "structure": 15}
    assert result.passed
    assert result.auto_publish_allowed


def test_閾値未満は不合格(reviewers):
    result = review(make_client(fact_score=10), reviewers, "本文",
                    pass_threshold=70, risk_tier="normal")
    assert result.score == 50
    assert not result.passed
    assert not result.auto_publish_allowed


def test_高リスクは合格しても自動公開不可(reviewers):
    """このテストが落ちる変更はマージしてはいけない。"""
    result = review(make_client(fact_score=50, clarity=30, structure=20),
                    reviewers, "本文", pass_threshold=70, risk_tier="high")
    assert result.score == 100  # 満点でも
    assert result.passed
    assert not result.auto_publish_allowed


def test_配点上限超過と不正値は集計を壊さない(reviewers):
    client = MockLLMClient({
        "[事実性レビュアー]": {"factuality": 999, "issues": []},   # 上限50に切る
        "[文章レビュアー]": {"clarity": "高い", "structure": -5, "issues": []},  # 0に落とす
    })
    result = review(client, reviewers, "本文", pass_threshold=70)
    assert result.breakdown == {"factuality": 50, "clarity": 0, "structure": 0}


def test_全レビュアーの指摘が集約される(reviewers):
    client = MockLLMClient({
        "[事実性レビュアー]": {"factuality": 40, "issues": ["出典が無い"]},
        "[文章レビュアー]": {"clarity": 20, "structure": 10, "issues": ["見出しが長い"]},
    })
    result = review(client, reviewers, "本文", pass_threshold=70)
    assert sorted(result.issues) == ["出典が無い", "見出しが長い"]


def test_未知のrisk_tierは拒否(reviewers):
    with pytest.raises(ValueError):
        review(make_client(), reviewers, "本文", pass_threshold=70, risk_tier="medium")


class TestExtractJson:
    def test_コードフェンス付き応答(self):
        assert extract_json('前置き\n```json\n{"a": 1}\n```') == {"a": 1}

    def test_JSONが無い応答はValueError(self):
        with pytest.raises(ValueError):
            extract_json("すみません、評価できませんでした。")
