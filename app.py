"""DevOps Incident Response Agent — Chainlit Entry Point.

Uses PydanticAI + Gemini + Strake MCP to investigate production incidents.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import chainlit as cl
from mcp import ClientSession
from mcp.client.sse import sse_client
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from strake_agent.agent import agent, Deps
from strake_agent.compact import (
    auto_compact,
    estimate_tokens,
)
from strake_agent.config import (
    AUTO_COMPACT_THRESHOLD,
    STRAKE_SSE_URL,
    TASK_REMINDER_ROUNDS,
    get_model_settings,
    logger,
)
from strake_agent.task_manager import TaskManager
from strake_agent.background_manager import BackgroundManager
from pathlib import Path
import json


@dataclass(frozen=True)
class StepInfo:
    """Represents a single step in the investigation process.

    Attributes:
        tool: The name of the tool being executed.
        detail: A brief description of what the tool is doing.
    """

    tool: str
    detail: str


# ── Global MCP connection (shared across all Chainlit sessions) ───────────────
_GLOBAL_MCP: dict[str, Any] = {
    "task": None,
    "session": None,
    "tools": [],
    "ready": asyncio.Event(),
    "error": None,
}
_MCP_LOCK = asyncio.Lock()


async def _mcp_worker() -> None:
    """Background task that keeps the Strake SSE connection alive."""
    logger.info("Starting MCP worker background task.")
    _GLOBAL_MCP["ready"].clear()

    while True:
        try:
            logger.info("Connecting to Strake MCP at %s", STRAKE_SSE_URL)
            async with sse_client(STRAKE_SSE_URL) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    mcp_tools = await session.list_tools()
                    async with _MCP_LOCK:
                        _GLOBAL_MCP["session"] = session
                        _GLOBAL_MCP["tools"] = mcp_tools.tools
                        _GLOBAL_MCP["error"] = None
                        _GLOBAL_MCP["ready"].set()
                    logger.info(
                        "MCP worker ready - %d tools available.", len(mcp_tools.tools)
                    )

                    while True:
                        # Keep-alive or periodic refresh if needed
                        await asyncio.sleep(30)
        except Exception as e:
            async with _MCP_LOCK:
                _GLOBAL_MCP["session"] = None
                _GLOBAL_MCP["tools"] = []
                _GLOBAL_MCP["ready"].clear()
                _GLOBAL_MCP["error"] = str(e)
            logger.error("MCP worker encountered an error: %s. Retrying in 5s...", e)
            await asyncio.sleep(5)


async def _current_mcp() -> tuple[Any, list[Any]]:
    async with _MCP_LOCK:
        return _GLOBAL_MCP["session"], list(_GLOBAL_MCP["tools"])


def _session_tasks_dir() -> Path:
    session_id = cl.user_session.get("task_session_id")
    if not session_id:
        session_id = uuid.uuid4().hex
        cl.user_session.set("task_session_id", session_id)
    return Path(".tasks") / session_id


async def set_investigating(active: bool, step: StepInfo | None = None) -> None:
    """Show or hide the per-session investigation status element.

    Args:
        active: True to create/update the element; False to finalize and remove it.
        step: Optional StepInfo describing the current investigation step.
    """
    investigating_msg = cl.user_session.get("investigating_msg")

    if active:
        if investigating_msg is None:
            # First call — create the element
            investigating_element = cl.CustomElement(
                name="InvestigationStatus",
                props={
                    "status": "running",
                    "current": "Starting investigation...",
                    "steps": [],
                },
                display="inline",
            )
            investigating_msg = cl.Message(
                content="", elements=[investigating_element], author="System"
            )
            await investigating_msg.send()
            cl.user_session.set("investigating_msg", investigating_msg)

        if step:
            element = investigating_msg.elements[0]
            steps: list = element.props.get("steps", [])

            # Mark previous in-progress step as done
            for s in steps:
                if not s.get("done"):
                    s["done"] = True

            steps.append({"tool": step.tool, "detail": step.detail, "done": False})
            element.props["steps"] = steps
            element.props["current"] = step.tool
            await element.update()

    else:
        if investigating_msg is not None:
            element = investigating_msg.elements[0]
            element.props["status"] = "done"
            await element.update()
            await asyncio.sleep(1.5)  # let the user see the completion briefly
            await investigating_msg.remove()
            cl.user_session.set("investigating_msg", None)


async def plan_confirmation_callback(rendered_plan: str) -> bool:
    """Request user confirmation for an investigation plan via Chainlit actions.

    Args:
        rendered_plan: The markdown-formatted plan or task list.

    Returns:
        True if the user confirmed the plan, False otherwise.
    """
    logger.debug("Prompting user for plan confirmation.")
    plan_markdown = f"### Investigation Strategy\n\n{rendered_plan}\n\n**Do you want to proceed with this plan?**"

    res = await cl.AskActionMessage(
        content=plan_markdown,
        actions=[
            cl.Action(
                name="confirm",
                value="confirm",
                label="✅ Proceed",
                theme="primary",
                payload={},
            ),
            cl.Action(name="reject", value="reject", label="❌ Refine", payload={}),
        ],
        author="System",
    ).send()

    confirmed = bool(res and res.get("name") == "confirm")
    logger.info(
        "User plan confirmation response: %s", "ACCEPTED" if confirmed else "REJECTED"
    )
    return confirmed


async def action_approval_callback(title: str, detail: str) -> bool:
    """Request user approval for a potentially destructive script."""
    res = await cl.AskActionMessage(
        content=f"### {title}\n\nThis script contains commands that could modify or delete data. Do you want to allow it?\n\n```python\n{detail}\n```",
        actions=[
            cl.Action(
                name="allow",
                value="allow",
                label="✅ Allow",
                theme="primary",
                payload={},
            ),
            cl.Action(name="deny", value="deny", label="🛑 Deny", payload={}),
        ],
        author="System",
    ).send()

    approved = bool(res and res.get("name") == "allow")
    logger.info(
        "User action approval response: %s", "ALLOWED" if approved else "DENIED"
    )
    return approved


# ── Chainlit lifecycle ────────────────────────────────────────────────────────
@cl.on_chat_start
async def on_chat_start() -> None:
    """Initialize a new chat session."""
    logger.info("Initializing new Chainlit session.")

    async with _MCP_LOCK:
        if _GLOBAL_MCP["task"] is None or _GLOBAL_MCP["task"].done():
            _GLOBAL_MCP["task"] = asyncio.create_task(_mcp_worker())

    # Ensure metadata is ready
    if not _GLOBAL_MCP["ready"].is_set():
        logger.info("Waiting for MCP worker to become ready...")

    mcp_session, mcp_tools = await _current_mcp()
    cl.user_session.set("history", [])
    cl.user_session.set(
        "deps",
        Deps(
            mcp_session=mcp_session,
            mcp_lock=_MCP_LOCK,
            mcp_tools=mcp_tools,
            todo_confirmation_callback=plan_confirmation_callback,
            set_investigating_callback=lambda active, step=None: set_investigating(
                active, StepInfo(**step) if step else None
            ),
            add_step_callback=lambda step: set_investigating(
                True, step=StepInfo(**step)
            ),
            action_approval_callback=action_approval_callback,
            tasks=TaskManager(_session_tasks_dir()),
            background=BackgroundManager(workdir=Path.cwd()),
        ),
    )

    await cl.Message(
        content="""🔴 **DevOps Support Agent** ready.

I can correlate across:
- **SQLite** — alert history (10,000 alerts)
- **Parquet** — time-series metrics (1M data points)
- **JSON** — deployment logs (200 deployments)
- **CSV** — on-call rotations (20 engineers)
- **REST** — git metadata (department information)

Try asking: *"What caused the API latency spike at 14:30 UTC?"*""",
        author="System",
    ).send()


async def _trigger_background_compaction(history: list[ModelMessage]) -> None:
    """Check if the context exceeds the threshold and trigger background compaction asynchronously.

    This runs out-of-band, updating the session history in the background without holding up the user.
    """
    try:
        token_count = estimate_tokens(history)
        if token_count > AUTO_COMPACT_THRESHOLD:
            logger.info(
                "⚡ Context at ~%d tokens — starting background compaction...",
                token_count,
            )
            new_history = await auto_compact(history)
            cl.user_session.set("history", new_history)
            logger.info("✅ Background context compaction complete.")
    except Exception as bg_err:
        logger.error("Background compaction failed: %s", bg_err)


async def _maybe_inject_nag(
    deps: Deps, history: list[ModelMessage]
) -> list[ModelMessage]:
    """Inject a task reminder into the history if tasks have been pending for too long."""
    if deps.rounds_since_todo >= TASK_REMINDER_ROUNDS and deps.tasks:
        ready_json = deps.tasks.list_ready()
        ready = json.loads(ready_json)
        if ready:
            nag = ModelRequest(
                parts=[
                    UserPromptPart(
                        content=f"<reminder>You have {len(ready)} pending tasks ready to start — update your progress or continue with the next task.</reminder>"
                    )
                ]
            )
            return history + [nag]
    return history


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Handle incoming user messages.

    Args:
        message: The user's message.
    """
    # Reliability check: ensure MCP is connected
    if not _GLOBAL_MCP["ready"].is_set():
        loading_msg = await cl.Message(
            content="⏳ Connecting to Strake…", author="System"
        ).send()
        try:
            await asyncio.wait_for(_GLOBAL_MCP["ready"].wait(), timeout=10.0)
            await loading_msg.remove()
        except asyncio.TimeoutError:
            loading_msg.content = "❌ Could not connect to Strake. Please retry."
            await loading_msg.update()
            return

    history: list[ModelMessage] = cl.user_session.get("history")
    deps: Deps = cl.user_session.get("deps")

    # Refresh MCP session in case worker reconnected
    deps.mcp_session, deps.mcp_tools = await _current_mcp()

    # Context management (Pre-run)
    history = await _maybe_inject_nag(deps, history)

    # ⚡ s08: Drain background notifications
    if deps.background:
        notifs = deps.background.drain_notifications()
        if notifs:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] Command `{n['command']}` finished.\nResult: {n['result']}"
                for n in notifs
            )
            history.append(
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content=f"<background-results>\n{notif_text}\n</background-results>"
                        )
                    ]
                )
            )

    await set_investigating(True)

    complete_text = ""
    new_history = history
    turn_usage = RunUsage()
    try:
        from pydantic_ai.usage import UsageLimits

        async with agent.run_stream(
            message.content,
            message_history=history,
            deps=deps,
            model_settings=get_model_settings(),
            usage_limits=UsageLimits(total_tokens_limit=150000),
        ) as result:
            async for chunk in result.stream_text(delta=True):
                complete_text += chunk

            # Capture version inside the context manager
            new_history = result.all_messages()
            turn_usage = result.usage()
    except Exception:
        logger.error("run_stream failed", exc_info=True)
        raise
    finally:
        await set_investigating(False)

    if complete_text:
        await cl.Message(content=complete_text).send()
    else:
        await cl.Message(
            content="Investigation complete. No summary text yielded."
        ).send()

    if deps.used_sources:
        sources_list = ", ".join(f"`{s}`" for s in sorted(deps.used_sources))
        await cl.Message(
            content=f"📊 **Sources used**: {sources_list}", author="System"
        ).send()

    # Handle manual compact tool call
    compact_requested = False
    for msg in new_history:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if (
                    isinstance(part, ToolReturnPart)
                    and str(part.content) == "__COMPACT_REQUESTED__"
                ):
                    compact_requested = True

    if compact_requested:
        comp_msg = await cl.Message(
            content="🗜️ Compressing history...", author="System"
        ).send()
        new_history = await auto_compact(new_history)
        comp_msg.content = "✅ Done. Transcript saved to `.transcripts/`"
        await comp_msg.update()

    deps.cumulative_usage += turn_usage
    deps.script_calls_this_turn = 0

    # Trigger background compaction out-of-band (out of user request thread)
    asyncio.create_task(_trigger_background_compaction(new_history))

    cl.user_session.set("history", new_history)
    cl.user_session.set("deps", deps)

    final_tokens = estimate_tokens(new_history)
    await cl.Message(
        content=f"_~{turn_usage.total_tokens:,} tokens this turn · "
        f"{final_tokens:,} tokens in context_",
        author="System",
    ).send()
