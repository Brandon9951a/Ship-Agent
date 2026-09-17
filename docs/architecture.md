# Architecture Notes

`agent_v2` starts with a small layered structure:

- `core` owns the agent orchestration flow.
- `tools` owns callable tool registration and lookup.
- `schemas` owns shared typed data structures.
- `ui` owns presentation and interaction surfaces.
- `config` stores environment-specific settings templates.

The first runnable path is intentionally narrow: `main.py` creates `AgentV2`, runs the `status` task, and renders the response through the console UI.
