import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional, Set

import chainlit as cl
from mcp.types import Tool as McpTool
from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import RunUsage

from .config import (
    MAX_TOOL_OUTPUT_CHARS,
    get_model,
    logger,
)
from .todo import TodoList, TodoItem
from .skills import SKILL_LOADER
from .compact import intra_run_compact


@dataclass
class Deps:
    """Dependency container for the DevOps Agent.

    Attributes:
        mcp_session: Live MCP ClientSession (injected at runtime).
        mcp_tools: List of available MCP tools.
        todos: The current investigation todo list.
        rounds_since_todo: Counter for turns since the last todo update.
        used_sources: Set of data sources (tables/APIs) accessed during the session.
        todo_confirmation_callback: Async callback to request user confirmation for a plan.
        set_investigating_callback: Async callback to toggle investigation UI status.
        add_step_callback: Async callback to record a new investigation step.
    """

    mcp_session: Any = None
    mcp_tools: list[McpTool] = field(default_factory=list)
    todos: TodoList = field(default_factory=TodoList)
    rounds_since_todo: int = 0
    used_sources: Set[str] = field(default_factory=set)
    # Interaction callbacks
    todo_confirmation_callback: Optional[Callable[[str], Awaitable[bool]]] = None
    set_investigating_callback: Optional[Callable[[bool], Awaitable[None]]] = None
    add_step_callback: Optional[Callable[[dict], Awaitable[None]]] = None
    cumulative_usage: RunUsage = field(default_factory=RunUsage)


agent = Agent(
    get_model("agent"),
    deps_type=Deps,
    history_processors=[intra_run_compact],
    instructions=(
        "You are a DevOps Agent. "
        "Your job is to root-cause production incidents by correlating data across:\n"
        "  • SQLite `sqlite.alerts`   — alert history (severity, service, acknowledged_by, runbook_id)\n"
        "  • Parquet `parquet.metrics`— time-series (timestamp, service, metric_name, value, pod_id)\n"
        "  • JSON   `json.deployments`— deploy logs (commit_sha, service, started_at, changed_files)\n"
        "  • `alerts`      — alert history (service, severity, triggered_at, resolved_at, acknowledged_by, runbook_id)\n"
        "  • `metrics`     — time-series (timestamp, service, metric_name, value, pod_id) — Parquet file\n"
        "  • `deployments` — deploy logs (deploy_id, service, commit_sha, started_at, duration, status) — JSON\n"
        "  • `oncall`      — on-call rota (engineer_id, primary_service, expertise, phone, avg_response_min) — CSV\n"
        "  • `git_deploys` — git metadata (deploy_id, commit_sha, engineer_id, username, department) — REST API\n\n"
        "To investigate, call `run_python_code` with a Python script. "
        "The `strake` library is pre-imported. "
        "IMPORTANT: The sandbox is restricted. Do NOT `import sys` or other sensitive modules. "
        "Use `print()` to output your results. The output will be automatically truncated if too long.\n"
        "Strake provides:\n"
        '  • `strake.sql("SELECT ...")` — returns a Table object.\n'
        '  • `strake.search("keyword")`  — returns schema metadata.\n'
        "  • `Table.to_pylist()`        — returns a list of dictionaries (rows).\n\n"
        "If a query returns an empty result or 0 rows, do NOT retry with minor variations. "
        "Instead, move to the next todo item or search for schema information first using search_schemas. "
        "An empty result is information — it means the data does not exist in that table.\n\n"
        "Example investigation script:\n"
        "```python\n"
        "import json\n"
        "# 1. Find the latency spike\n"
        "res = strake.sql(\"SELECT * FROM metrics WHERE metric_name = 'api_latency_p99' AND value > 1000\")\n"
        "rows = res.to_pylist()\n"
        'print(f"Found {len(rows)} spike points")\n'
        "\n"
        "# 2. Correlate with deployments\n"
        "if rows:\n"
        "    t = rows[0]['timestamp']\n"
        "    deps = strake.sql(f\"SELECT * FROM deployments WHERE started_at <= '{t}' ORDER BY started_at DESC LIMIT 1\")\n"
        '    print("Recent deployment:", deps.to_pylist())\n'
        "```"
    ),
)


@agent.instructions
def skill_listing(_ctx: RunContext[Deps]) -> str:
    """Model instruction to list available agent skills.

    Returns:
        A string listing available skills or an empty string if none.
    """
    listing = SKILL_LOADER.listing()
    if listing == "No skills available.":
        return ""
    return "\n\n" + listing


@agent.instructions
async def nag_reminder(ctx: RunContext[Deps]) -> str:
    """Model instruction to remind the agent about pending tasks.

    Args:
        ctx: The run context containing dependencies.

    Returns:
        A reminder string if tasks are pending for too long, else an empty string.
    """
    if ctx.deps.rounds_since_todo >= 3 and ctx.deps.todos.items:
        return "\n<reminder>You have pending todos — update your todo list.</reminder>"
    return ""


def _extract_used_sources(script: str) -> Set[str]:
    """Identify data sources mentioned in the script.

    Args:
        script: The Python script to analyze.

    Returns:
        A set of source names identified in the script.
    """
    KNOWN_TABLES = {"alerts", "metrics", "deployments", "oncall", "git_deploys"}
    found = set()
    for table in KNOWN_TABLES:
        # Check for table name as a word, likely used in a query
        # Using a more specific pattern to avoid false positives in comments
        if re.search(
            rf"strake\.sql\(.*?\b{table}\b", script, re.IGNORECASE | re.DOTALL
        ):
            found.add(table)
    return found


@agent.tool
async def run_python_code(ctx: RunContext[Deps], script: str) -> str:
    """Execute a Python script using the `strake` library to investigate data.

    Args:
        ctx: The run context.
        script: The Python script to execute in the Strake sandbox.

    Returns:
        The output of the script or an error message.
    """
    if not ctx.deps.mcp_session:
        return "Error: Strake MCP session not initialized."

    logger.debug("Executing script: %s", script)

    async with cl.Step(name="run_python_code", type="tool") as step:
        step.input = script
        if ctx.deps.add_step_callback:
            await ctx.deps.add_step_callback(
                {"tool": "run_python_code", "detail": "executing investigation script"}
            )
        try:
            result = await ctx.deps.mcp_session.call_tool(
                "run_python", {"script": script}
            )
            texts = []
            for content in getattr(result, "content", []):
                if hasattr(content, "text"):
                    texts.append(content.text)
                elif isinstance(content, dict) and "text" in content:
                    texts.append(content["text"])
                else:
                    texts.append(str(content))

            output = "\n".join(texts) or "(empty result)"
            if len(output) > MAX_TOOL_OUTPUT_CHARS:
                output = (
                    output[:MAX_TOOL_OUTPUT_CHARS]
                    + f"\n...(truncated, {len(output)} total chars)"
                )

            step.output = output
            if len(output) <= 10:
                logger.warning(
                    "Script returned suspiciously short output (%d chars): %r — possible empty result or error",
                    len(output),
                    output,
                )
            else:
                logger.info(
                    "Script execution succeeded. Output length: %d", len(output)
                )

            # Track used sources
            ctx.deps.used_sources.update(_extract_used_sources(script))

            res = output
        except Exception as e:
            step.output = f"Error: {e}"
            logger.error("Script execution failed: %s", e)
            res = f"Execution error: {e}"
    return res


@agent.tool
async def search_schemas(ctx: RunContext[Deps], keyword: str) -> str:
    """Search available tables and column names matching the given keyword.

    Args:
        ctx: The run context.
        keyword: The search term for schema lookup.

    Returns:
        Matching schema information or an error message.
    """
    if not ctx.deps.mcp_session:
        return "Error: Strake MCP session not initialized."

    logger.debug("Searching schemas for keyword: %s", keyword)
    async with cl.Step(name="search_schemas", type="tool") as step:
        step.input = keyword
        if ctx.deps.add_step_callback:
            await ctx.deps.add_step_callback(
                {"tool": "search_schemas", "detail": f"searching for '{keyword}'"}
            )
        try:
            result = await ctx.deps.mcp_session.call_tool(
                "search_schemas", {"query": keyword}
            )
            texts = []
            for content in getattr(result, "content", []):
                if hasattr(content, "text"):
                    texts.append(content.text)
                else:
                    texts.append(str(content))
            output = "\n".join(texts) or "No matching schemas."
            step.output = output
            logger.info("Schema search complete.")
            res = output
        except Exception as e:
            step.output = f"Error: {e}"
            logger.error("Schema search failed: %s", e)
            res = f"Schema search error: {e}"
    return res


@agent.tool
async def todo(ctx: RunContext[Deps], items: list[dict]) -> str:
    """Update the investigation todo list.

    Args:
        ctx: The run context.
        items: List of todo items as dictionaries.
               Each item: {id: str, text: str, status: pending|in_progress|done}.

    Returns:
        The rendered todo list or a rejection message.
    """
    ret_val = ""
    async with cl.Step(name="todo", type="tool") as step:
        ctx.deps.rounds_since_todo = 0
        parsed = [TodoItem(**i) for i in items]

        has_new_work = any(i.status in ("pending", "in_progress") for i in parsed)

        # Key items by id for reliable comparison
        old_by_id = {i.id: i for i in ctx.deps.todos.items}

        # A plan is "done only" if no items were added/reordered, and the only change is setting some items to done
        is_marking_done_only = all(
            p.status == "done"
            or (p.id in old_by_id and p.status == old_by_id[p.id].status)
            for p in parsed
        ) and not any(p.id not in old_by_id for p in parsed)

        ctx.deps.todos = TodoList(items=parsed)
        rendered = ctx.deps.todos.render()
        step.input = str(items)
        step.output = rendered

        should_confirm = has_new_work and not is_marking_done_only

        if ctx.deps.todo_confirmation_callback and should_confirm:
            if ctx.deps.set_investigating_callback:
                await ctx.deps.set_investigating_callback(False)

            logger.info("Waiting for user confirmation of investigation plan.")
            confirmed = await ctx.deps.todo_confirmation_callback(rendered)

            if not confirmed:
                logger.info("User rejected the plan.")
                ret_val = "User REJECTED this plan. Please refine your strategy, ask for clarification, or try a simpler approach."
            else:
                if ctx.deps.set_investigating_callback:
                    await ctx.deps.set_investigating_callback(True)
                logger.info("User confirmed the plan.")

        if not ret_val:
            logger.info("Todo list updated: %d items", len(parsed))
            ret_val = rendered

    return ret_val


@agent.tool
async def load_skill(_ctx: RunContext[Deps], name: str) -> str:
    """Load the full body of a named skill for workflow guidance.

    Args:
        _ctx: The run context.
        name: Name of the skill to load.

    Returns:
        The content of the skill or an error message.
    """
    return SKILL_LOADER.load(name)


@agent.tool
async def compact(_ctx: RunContext[Deps]) -> str:
    """Manually compress the conversation history when context is getting long.

    Returns:
        A sentinel string to trigger compaction in the application layer.
    """
    return "__COMPACT_REQUESTED__"
