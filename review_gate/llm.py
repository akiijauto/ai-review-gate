"""LLMクライアント。既定はGemini。テストはMockLLMClientで完結する。

レビュー用途では応答が厳密なJSONで返らないことがあるため、
テキストから最初のJSONオブジェクトを抽出してパースする。
503（過負荷）のみ指数バックオフで再試行し、4xxは即座に失敗させる。
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod


class LLMClient(ABC):
    @abstractmethod
    def generate_json(self, prompt: str) -> dict:
        """プロンプトを送り、応答からJSONオブジェクトを取り出して返す。"""


def extract_json(text: str) -> dict:
    """応答テキストから最初のJSONオブジェクトを抽出する。

    コードフェンス付き・前置きの説明文付きの応答でも取り出せるようにする。
    抽出できない応答は上流で再生成を判断できるよう ValueError にする。
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"応答からJSONを抽出できませんでした: {text[:200]}")
    return json.loads(match.group(0))


class GeminiClient(LLMClient):
    def __init__(self, model: str = "gemini-2.5-flash") -> None:
        # 遅延importにより、テスト実行にgoogle-genai・tenacityのインストールを不要にする
        # （テストは MockLLMClient だけで完結する）
        import tenacity
        from google import genai
        from google.genai import errors as genai_errors

        self._tenacity = tenacity
        self._genai_errors = genai_errors
        self._client = genai.Client()
        self._model = model

    def _is_retryable(self, exc: Exception) -> bool:
        return isinstance(exc, self._genai_errors.ServerError) and exc.code == 503

    def generate_json(self, prompt: str) -> dict:
        t = self._tenacity

        @t.retry(
            retry=t.retry_if_exception(self._is_retryable),
            wait=t.wait_exponential(multiplier=10, min=10, max=80),
            stop=t.stop_after_attempt(4),
            reraise=True,
        )
        def _call() -> dict:
            response = self._client.models.generate_content(
                model=self._model, contents=prompt
            )
            return extract_json(response.text)

        return _call()


class MockLLMClient(LLMClient):
    """テスト用。プロンプト中の部分文字列で応答を切り替える。"""

    def __init__(self, responses: dict[str, dict]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def generate_json(self, prompt: str) -> dict:
        self.calls.append(prompt)
        for marker, response in self._responses.items():
            if marker in prompt:
                return response
        raise AssertionError("どのマーカーにも一致しないプロンプトが送られました")
