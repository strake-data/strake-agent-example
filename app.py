"""DevOps Incident Response Agent — Chainlit Entry Point.

Uses PydanticAI + Gemini + Strake MCP to investigate production incidents.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import chainlit as cl
from mcp import ClientSession
from mcp.client.sse import sse_client
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
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
    get_model_settings,
    logger,
)


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
                    _GLOBAL_MCP["session"] = session
                    _GLOBAL_MCP["tools"] = mcp_tools.tools
                    _GLOBAL_MCP["ready"].set()
                    logger.info(
                        "MCP worker ready - %d tools available.", len(mcp_tools.tools)
                    )

                    while True:
                        # Keep-alive or periodic refresh if needed
                        await asyncio.sleep(30)
        except Exception as e:
            _GLOBAL_MCP["ready"].clear()
            _GLOBAL_MCP["error"] = str(e)
            logger.error("MCP worker encountered an error: %s. Retrying in 5s...", e)
            await asyncio.sleep(5)


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


async def todo_confirmation_callback(rendered_todo: str) -> bool:
    """Request user confirmation for an investigation plan via Chainlit actions.

    Args:
        rendered_todo: The markdown-formatted todo list.

    Returns:
        True if the user confirmed the plan, False otherwise.
    """
    logger.debug("Prompting user for plan confirmation.")
    plan_markdown = f"### Investigation Strategy\n\n{rendered_todo}\n\n**Do you want to proceed with this plan?**"

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

    cl.user_session.set("history", [])
    cl.user_session.set(
        "deps",
        Deps(
            mcp_session=_GLOBAL_MCP["session"],
            mcp_tools=_GLOBAL_MCP["tools"],
            todo_confirmation_callback=todo_confirmation_callback,
            set_investigating_callback=lambda active: set_investigating(active),
            add_step_callback=lambda step: set_investigating(
                True, step=StepInfo(**step)
            ),
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


async def _check_and_compact(history: list[ModelMessage]) -> list[ModelMessage]:
    """Check if the context exceeds the threshold and trigger auto-compaction if so.

    Args:
        history: Current conversation history.

    Returns:
        The (possibly compacted) history.
    """
    token_count = estimate_tokens(history)
    if token_count > AUTO_COMPACT_THRESHOLD:
        status_msg = await cl.Message(
            content=f"⚡ Context at ~{token_count:,} tokens — auto-compressing...",
            author="System",
        ).send()
        new_history = await auto_compact(history)
        status_msg.content = "✅ Compressed. Transcript saved."
        await status_msg.update()
        return new_history
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
    deps.mcp_session = _GLOBAL_MCP["session"]

    # Context management (Pre-run)
    history = await _check_and_compact(history)

    await set_investigating(True)

    complete_text = ""
    new_history = []
    turn_usage = RunUsage()
    try:
        async with agent.run_stream(
            message.content,
            message_history=history,
            deps=deps,
            model_settings=get_model_settings(),
        ) as result:
            async for chunk in result.stream_text(delta=True):
                complete_text += chunk

            # Capture version inside the context manager
            new_history = result.all_messages()
            turn_usage = result.usage()
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
    new_history = await _check_and_compact(new_history)

    cl.user_session.set("history", new_history)
    cl.user_session.set("deps", deps)

    final_tokens = estimate_tokens(new_history)
    await cl.Message(
        content=f"_~{turn_usage.total_tokens:,} tokens this turn · "
        f"{estimate_tokens(new_history):,} tokens in context_",
        author="System",
    ).send()
