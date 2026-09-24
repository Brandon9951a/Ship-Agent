"""Persistent LangGraph runtime used by the web application."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from core.llm_layer import LLMClient
from core.orchestrator import AgentState, build_workflow


class WorkflowNotFoundError(LookupError):
    pass


class WorkflowNotAwaitingChoiceError(RuntimeError):
    pass


class InvalidAdjustmentOptionError(ValueError):
    pass


def validate_thread_id(value: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("thread_id must be a UUID") from exc
    return str(parsed)


def _without_runtime_markers(state: dict[str, Any]) -> AgentState:
    clean = dict(state)
    clean.pop("__interrupt__", None)
    return clean  # type: ignore[return-value]


class WorkflowRuntime:
    """Own one compiled graph and serialize resumes per workflow thread."""

    def __init__(
        self,
        checkpointer: Any,
        *,
        checkpoint_backend: str,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.checkpoint_backend = checkpoint_backend
        self.checkpoint_ready = True
        self.graph = build_workflow(
            llm_client=llm_client,
            checkpointer=checkpointer,
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    async def _lock_for(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(thread_id, asyncio.Lock())

    @staticmethod
    def _initial_state(thread_id: str) -> AgentState:
        return {
            "trace": [],
            "tool_results": [],
            "decision_history": [],
            "tool_mode": "synthetic_demo",
            "thread_id": thread_id,
            "replan_count": 0,
            "max_replans": 2,
            "replan_limit_reached": False,
        }

    async def start_text(self, task_text: str) -> AgentState:
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("task_text must be non-empty text")
        thread_id = str(uuid4())
        initial = self._initial_state(thread_id)
        initial["user_input"] = task_text.strip()
        result = await self.graph.ainvoke(initial, self._config(thread_id))
        return _without_runtime_markers(result)

    async def start_structured(self, payload: dict[str, Any]) -> AgentState:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        thread_id = str(uuid4())
        initial = self._initial_state(thread_id)
        initial["request"] = payload
        result = await self.graph.ainvoke(initial, self._config(thread_id))
        return _without_runtime_markers(result)

    async def get_state(self, thread_id: str) -> AgentState:
        thread_id = validate_thread_id(thread_id)
        snapshot = await self.graph.aget_state(self._config(thread_id))
        if not snapshot.values:
            raise WorkflowNotFoundError(thread_id)
        return _without_runtime_markers(dict(snapshot.values))

    async def resume(
        self,
        thread_id: str,
        decision_id: str,
        option_id: str,
    ) -> AgentState:
        thread_id = validate_thread_id(thread_id)
        lock = await self._lock_for(thread_id)
        async with lock:
            snapshot = await self.graph.aget_state(self._config(thread_id))
            if not snapshot.values:
                raise WorkflowNotFoundError(thread_id)
            state = dict(snapshot.values)
            if (
                state.get("status") != "awaiting_choice"
                or "await_choice" not in snapshot.next
                or state.get("pending_decision_id") != decision_id
            ):
                raise WorkflowNotAwaitingChoiceError(thread_id)
            options = {
                str(item.get("option_id")): item
                for item in state.get("adjustment_options", [])
            }
            if option_id not in options:
                raise InvalidAdjustmentOptionError(option_id)
            resume_value = {
                "decision_id": decision_id,
                "option_id": option_id,
                "selected_at": datetime.now(timezone.utc).isoformat(),
            }
            result = await self.graph.ainvoke(
                Command(resume=resume_value),
                self._config(thread_id),
            )
            return _without_runtime_markers(result)


@asynccontextmanager
async def open_workflow_runtime(
    *,
    llm_client: LLMClient | None = None,
    backend: str | None = None,
    database_url: str | None = None,
    sqlite_path: str | Path | None = None,
) -> AsyncIterator[WorkflowRuntime]:
    """Open the configured checkpointer for the full application lifetime."""
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    selected = (backend or os.getenv("SHIP_CHECKPOINT_BACKEND") or "sqlite").lower()
    if selected == "memory":
        yield WorkflowRuntime(
            InMemorySaver(), checkpoint_backend="memory", llm_client=llm_client,
        )
        return

    if selected == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        configured = sqlite_path or os.getenv("SHIP_CHECKPOINT_SQLITE_PATH")
        path = Path(configured) if configured else Path(__file__).resolve().parents[1] / "var/checkpoints.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            await saver.setup()
            yield WorkflowRuntime(
                saver, checkpoint_backend="sqlite", llm_client=llm_client,
            )
        return

    if selected == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        uri = database_url or os.getenv("SHIP_CHECKPOINT_DATABASE_URL")
        if not uri:
            raise RuntimeError("SHIP_CHECKPOINT_DATABASE_URL is required for postgres")
        async with AsyncPostgresSaver.from_conn_string(uri) as saver:
            await saver.setup()
            yield WorkflowRuntime(
                saver, checkpoint_backend="postgres", llm_client=llm_client,
            )
        return

    raise RuntimeError(f"unsupported checkpoint backend: {selected}")
