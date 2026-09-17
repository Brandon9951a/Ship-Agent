from collections.abc import Callable

from schemas.messages import ToolStepResult
from tools.placeholders import (
    run_tdata,
    run_tenergy,
    run_tmanagement,
    run_tseg,
    run_tspeed,
)


ToolStep = Callable[[], ToolStepResult]


TOOL_CHAIN: tuple[ToolStep, ...] = (
    run_tdata,
    run_tseg,
    run_tenergy,
    run_tspeed,
    run_tmanagement,
)


def run_placeholder_pipeline() -> list[ToolStepResult]:
    return [step() for step in TOOL_CHAIN]
