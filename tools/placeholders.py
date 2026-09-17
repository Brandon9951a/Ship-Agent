from schemas.messages import ToolStepResult


def run_tdata() -> ToolStepResult:
    return ToolStepResult(name="Tdata", status="ok", detail="data acquisition placeholder completed")


def run_tseg() -> ToolStepResult:
    return ToolStepResult(name="Tseg", status="ok", detail="segment division placeholder completed")


def run_tenergy() -> ToolStepResult:
    return ToolStepResult(name="Tenergy", status="ok", detail="energy prediction placeholder completed")


def run_tspeed() -> ToolStepResult:
    return ToolStepResult(name="Tspeed", status="ok", detail="speed optimization placeholder completed")


def run_tmanagement() -> ToolStepResult:
    return ToolStepResult(name="Tmanagement", status="ok", detail="energy management placeholder completed")
