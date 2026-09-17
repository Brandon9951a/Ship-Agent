from schemas.messages import AgentResponse
from tools.registry import ToolRegistry


class AgentV2:
    def __init__(self, tools: ToolRegistry | None = None) -> None:
        self.tools = tools or ToolRegistry()

    def run(self, task: str) -> AgentResponse:
        if task == "status":
            return AgentResponse(message="agent_v2 is ready.", ok=True)

        return AgentResponse(message=f"Received task: {task}", ok=True)
