from core.agent import AgentV2
from ui.console import render_response


def main() -> None:
    agent = AgentV2()
    response = agent.run("status")
    render_response(response)


if __name__ == "__main__":
    main()
