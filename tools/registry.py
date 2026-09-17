from collections.abc import Callable
from typing import Any


ToolCallable = Callable[..., Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolCallable] = {}

    def register(self, name: str, tool: ToolCallable) -> None:
        self._tools[name] = tool

    def get(self, name: str) -> ToolCallable | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)
