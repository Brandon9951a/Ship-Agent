from schemas.messages import AgentResponse


def render_response(response: AgentResponse) -> None:
    print(response.message)
