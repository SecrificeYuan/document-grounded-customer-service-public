from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from docqa.llm_client import ModelReply, ModelRequest


class FakeLLMClient:
    def __init__(self, scripted: Sequence[ModelReply | Exception]) -> None:
        self.scripted = deque(scripted)
        self.calls: list[ModelRequest] = []

    def request(self, request: ModelRequest) -> ModelReply:
        self.calls.append(request)
        if not self.scripted:
            raise AssertionError("unexpected model request")
        result = self.scripted.popleft()
        if isinstance(result, Exception):
            raise result
        return result
