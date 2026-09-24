from dataclasses import dataclass


@dataclass(frozen=True)
class ToolStepResult:
    name: str
    status: str
    detail: str
