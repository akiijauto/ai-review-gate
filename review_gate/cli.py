"""CLI: 文書ファイルをレビューし、結果を表示・保存する。

使い方:
    python -m review_gate.cli 記事.md
    python -m review_gate.cli 記事.md --risk-tier high
    python -m review_gate.cli 記事.md --out reviews/記事.review.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .core import format_report, load_reviewers, review, save_result
from .llm import GeminiClient

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """`.env` を環境変数へ読み込む。

    APIキーはこのコードが直接読むのではなく、google-genai が環境変数
    （GEMINI_API_KEY / GOOGLE_API_KEY）から拾う。そのため `.env` を置いただけでは
    鍵が渡らず、これまでは自分で export する必要があった。
    READMEが `cp .env.example .env` と案内している以上、置けば動くようにする。

    python-dotenv が無くても CLI は動かす。テストは MockLLMClient で完結しており、
    この依存を必要としないため（google-genai / tenacity を遅延importにしているのと同じ理由）。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # 引数なしの load_dotenv() は「呼び出し元ファイルの場所」を起点に探すため、
    # 実行したディレクトリの .env を拾わない（2026-09-04 に実測）。場所を明示する。
    # 既に環境変数にある値は上書きしない（明示的に渡した値のほうが強い）。
    for candidate in (Path.cwd() / ".env", ROOT / ".env"):
        if candidate.is_file():
            load_dotenv(candidate)


def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="マルチレビュアー品質ゲート")
    parser.add_argument("document", help="レビュー対象の文書ファイル")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--risk-tier", choices=["normal", "high"], default=None,
                        help="省略時はconfigのdefault_risk_tier")
    parser.add_argument("--out", help="結果JSONの保存先（省略時は保存しない）")
    args = parser.parse_args()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    reviewers = load_reviewers(config, Path(args.config).resolve().parent)
    document = Path(args.document).read_text(encoding="utf-8")

    result = review(
        GeminiClient(model=config.get("model", "gemini-2.5-flash")),
        reviewers,
        document,
        pass_threshold=int(config["pass_threshold"]),
        risk_tier=args.risk_tier or config.get("default_risk_tier", "normal"),
    )

    print(format_report(result))
    if args.out:
        save_result(result, Path(args.out))
        print(f"保存先: {args.out}")

    # CIでゲートとして使えるように、不合格は終了コードで伝える
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
