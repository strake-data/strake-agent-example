import asyncio
import hashlib
import logging
import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional, Set, Literal

import chainlit as cl
from mcp.types import Tool as McpTool
from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import RunUsage

from .config import (
    MAX_TOOL_OUTPUT_CHARS,
    SCRIPT_EFFICIENCY_CRITICAL_CALL,
    SCRIPT_EFFICIENCY_WARNING_CALL,
    SCRIPT_SHORT_OUTPUT_WARN_CHARS,
    TASK_REMINDER_ROUNDS,
    get_model,
    logger,
)
from .skills import SKILL_LOADER
from .compact import intra_run_compact
from .task_manager import TaskManager
from .background_manager import BackgroundManager


# Shared settings/model configuration
MODEL_NAME = "agent"


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
    mcp_lock: Optional[asyncio.Lock] = None
    mcp_tools: list[McpTool] = field(default_factory=list)
    rounds_since_todo: int = 0
    used_sources: Set[str] = field(default_factory=set)
    # Interaction callbacks
    todo_confirmation_callback: Optional[Callable[[str], Awaitable[bool]]] = None
    set_investigating_callback: Optional[
        Callable[[bool, Optional[dict]], Awaitable[None]]
    ] = None
    add_step_callback: Optional[Callable[[dict], Awaitable[None]]] = None
    cumulative_usage: RunUsage = field(default_factory=RunUsage)
    tasks: Optional[TaskManager] = None
    background: Optional[BackgroundManager] = None
    is_child: bool = False
    script_calls_this_turn: int = 0
    script_output_cache: dict = field(
        default_factory=dict
    )  # cross-turn cache keyed by script hash
    sql_query_cache: dict = field(
        default_factory=dict
    )  # cross-run persistent SQL query cache
    action_approval_callback: Optional[Callable[[str, str], Awaitable[bool]]] = None
    recursion_depth: int = 0
    max_recursion_depth: int = 5
    recursion_trace: list = field(default_factory=list)  # For debugging


agent = Agent(
    get_model("agent"),
    deps_type=Deps,
    history_processors=[intra_run_compact],
    retries=5,
    instructions=(
        """
You are a DevOps RLM (Recursive Language Model) agent with access to a clean, well-modeled schema.

━━━ DATA MODEL CONTEXT ━━━
Five tables are available. The schema is "AI-ready" — names are intuitive,
joins are straightforward. You do NOT need extensive documentation.

    alerts    metrics    deployments    oncall    git_deploys

━━━ EXPECTED WORKFLOW (6-7 tool calls typical) ━━━

1. PROBE (1-2 calls)
   - search_schemas() for unfamiliar columns only
   - Quick COUNT/sample query to understand volume

2. EXPLORE (2-3 calls)  
   - Fetch relevant slices with targeted SQL
   - Use Python to join/filter/aggregate
   - This is the core "code mode" work

3. RECURSE (0-2 calls, only when needed)
   - Use recurse(data_slice, question) for SEMANTIC analysis
   - Don't recurse for mechanical operations (counting, filtering)

4. REVIEW (1 call)
   - Use review_result() to sanity check findings before finalizing
   - Cross-reference with a second source if low confidence

5. FINALIZE (1 call)
   - Call finalize() once the review is passed

━━━ ANTI-PATTERNS TO AVOID ━━━
✗ Fetching all tables upfront (probe first, fetch targeted)
✗ One SQL query per script with no Python logic
✗ Recursing for simple aggregations (do in Python)
✗ Over-documenting obvious columns
✗ Skipping the review step

━━━ WHAT "CODE MODE" MEANS ━━━
A good script:
  - 1-2 precise SQL fetches with strict WHERE filters pushed down to the source database
  - Standardized timestamp and ID filters to maximize Strake's row-group skip cache
  - Python joins/filters/aggregations
  - print() structured output
"""
    ),
)


child_agent = Agent(
    get_model(MODEL_NAME),
    deps_type=Deps,
    retries=5,
    instructions=(
        "You are a DevOps Subagent. Your job is to answer ONE specific technical question "
        "by querying data and returning a concise factual summary.\n\n"
        "### Code-Mode Rules (same as parent — follow these strictly)\n"
        "• Fetch broad data in 1-2 SQL calls, then join/filter/aggregate in Python.\n"
        "• NEVER call strake.sql() inside a for/while loop — fetch the full table first, "
        "  then use a dict for lookups.\n"
        "• Use timestamp formats exactly: metrics='YYYY-MM-DD HH:MM:SS', "
        "  deployments='YYYY-MM-DDTHH:MM:SS' (with T).\n\n"
        "### Output Format\n"
        "Return a short structured summary: key findings, numbers, service names. "
        "No preamble. Your context is discarded after you return — be concise."
    ),
)


_PRELUDE_FILE = Path(__file__).parent / "sandbox_prelude.py"
_GUARDRAIL_PREAMBLE = _PRELUDE_FILE.read_text()


def _prepend_guardrails(script: str) -> str:
    return _GUARDRAIL_PREAMBLE + "\n" + script


def _looks_destructive(script: str) -> bool:
    """Simple heuristic to detect potentially destructive operations."""
    patterns = [
        "DELETE",
        "DROP",
        "UPDATE",
        "os.remove",
        "os.rmdir",
        "shutil.rmtree",
        "subprocess",
    ]
    return any(p in script for p in patterns)


async def _check_approval(ctx: RunContext[Deps], script: str) -> Optional[str]:
    """Check if the script needs user approval and request it if so."""
    if ctx.deps.action_approval_callback and _looks_destructive(script):
        logger.info("Script looks destructive, requesting user approval.")
        if ctx.deps.set_investigating_callback:
            await ctx.deps.set_investigating_callback(False, None)

        approved = await ctx.deps.action_approval_callback(
            "Script approval required", script
        )

        if ctx.deps.set_investigating_callback:
            await ctx.deps.set_investigating_callback(True, None)

        if not approved:
            logger.warning("Script rejected by user.")
            return "Script execution REJECTED by user. Do not attempt to run destructive commands without explicit permission."
    return None


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


async def _call_mcp_tool(
    ctx: RunContext[Deps], tool_name: str, args: dict[str, Any]
) -> Any:
    if not ctx.deps.mcp_session:
        raise RuntimeError("Strake MCP session not initialized.")

    if ctx.deps.mcp_lock:
        async with ctx.deps.mcp_lock:
            return await ctx.deps.mcp_session.call_tool(tool_name, args)
    return await ctx.deps.mcp_session.call_tool(tool_name, args)


def _analyze_script_quality(script: str) -> list[str]:
    """Return a list of hint strings if the script looks like a SQL wrapper."""
    hints = []

    # Count strake.sql() calls
    sql_calls = len(re.findall(r"strake\.sql\s*\(", script))

    # Detect Python logic beyond print/assignment: loops, comprehensions, if-statements
    has_logic = bool(
        re.search(
            r"\bfor\b|\bif\b|\bwhile\b|\[.*for.*in\b|\.groupby\b|defaultdict|Counter\b",
            script,
        )
    )

    # Detect single-column SELECT * patterns (broad schema probe)
    schema_probes = len(
        re.findall(
            r"SELECT\s+(?:DISTINCT\s+)?(?:timestamp|metric_name|\*)\s+FROM\s+\w+\s+(?:LIMIT|ORDER)",
            script,
            re.IGNORECASE,
        )
    )

    # Detect timestamp format hunting (WHERE timestamp LIKE or multiple narrow time windows)
    ts_probe = bool(
        re.search(r"timestamp\s+LIKE\s+['\"]|started_at\s+LIKE", script, re.IGNORECASE)
    )

    if sql_calls == 1 and not has_logic:
        hints.append(
            f"⚠ SCRIPT QUALITY: This script issued 1 SQL query with no Python analysis. "
            f"For your NEXT script, fetch a broader dataset and process it in Python "
            f"(joins, filtering, aggregation). Avoid calling run_python_code for a single SELECT."
        )
    elif sql_calls > 1 and not has_logic:
        hints.append(
            f"⚠ SCRIPT QUALITY: {sql_calls} SQL queries, no Python logic. "
            f"Consider replacing the follow-up queries with Python joins on the data already fetched."
        )

    if ts_probe:
        hints.append(
            "⚠ TIMESTAMP FORMAT PROBE DETECTED: Do not guess timestamp formats with LIKE. "
            "Call search_schemas('timestamp') first to see actual sample values and the stored format."
        )

    if schema_probes >= 2:
        hints.append(
            "⚠ SCHEMA EXPLORATION via SQL: You are using SQL to discover schema/data ranges. "
            "Use search_schemas() for this — it is cheaper and does not spin up a sandbox."
        )

    # Detect strake.sql() call inside a for loop body (indented sql call)
    # This matches `    strake.sql(` with any leading whitespace
    indented_sql = re.findall(r"^[ \t]+strake\.sql\s*\(", script, re.MULTILINE)
    if indented_sql:
        hints.append(
            "⚠ N+1 QUERY DETECTED: strake.sql() is being called inside an indented block "
            "(likely a loop). This will issue one query per row. "
            "Fix: fetch the full related table once before the loop, then use a Python dict for lookups.\n"
            "BAD:  for row in rows:\n"
            "          info = strake.sql(f\"SELECT * FROM t WHERE id='{row['id']}'\").to_pylist()\n"
            "GOOD: all_info = {r['id']: r for r in strake.sql('SELECT * FROM t').to_pylist()}\n"
            "      for row in rows:\n"
            "          info = all_info.get(row['id'])"
        )

    return hints


async def _create_recurse_handler(ctx: RunContext[Deps]):
    """Factory to create a recurse() handler bound to current context."""

    recursion_depth = getattr(ctx.deps, "recursion_depth", 0)
    max_recursion_depth = getattr(ctx.deps, "max_recursion_depth", 5)

    async def handle_recurse(context_str: str, query: str, max_tokens: int) -> str:
        if recursion_depth >= max_recursion_depth:
            return f"[MAX RECURSION DEPTH {max_recursion_depth} REACHED - Summarize directly]"

        # Create a focused sub-agent call
        sub_prompt = f"""You are analyzing a specific data slice. 
        
DATA SLICE:
```
{context_str}
```

QUERY: {query}

Respond with ONLY the answer. Be concise and factual."""

        sub_deps = Deps(
            mcp_session=ctx.deps.mcp_session,
            mcp_lock=ctx.deps.mcp_lock,
            recursion_depth=recursion_depth + 1,
            max_recursion_depth=max_recursion_depth,
            is_child=True,
            mcp_tools=ctx.deps.mcp_tools,
            used_sources=ctx.deps.used_sources,
            todo_confirmation_callback=ctx.deps.todo_confirmation_callback,
            set_investigating_callback=ctx.deps.set_investigating_callback,
            add_step_callback=ctx.deps.add_step_callback,
            tasks=ctx.deps.tasks,
            background=ctx.deps.background,
        )

        result = await child_agent.run(sub_prompt, deps=sub_deps)
        return result.data

    return handle_recurse


@agent.tool
@child_agent.tool
async def run_python_code(ctx: RunContext[Deps], script: str) -> str:
    """Execute a Python script in the Strake sandbox for data analysis.

    Use CODE MODE: fetch data with strake.sql(), then join, filter, and aggregate
    in Python. A good script has 1-2 broad SQL fetches followed by Python logic.
    Do NOT call this tool just to run a single SELECT statement.
    """
    if not ctx.deps.mcp_session:
        return "Error: Strake MCP session not initialized."

    logger.debug("Executing script: %s", script)

    # 🛑 Check for mid-task approval if destructive
    approval_error = await _check_approval(ctx, script)
    if approval_error:
        return approval_error

    # Emit hints BEFORE execution so the model can see them even if the script fails
    quality_hints = _analyze_script_quality(script)
    ctx.deps.script_calls_this_turn += 1
    call_n = ctx.deps.script_calls_this_turn

    # Check cross-turn output cache (avoids re-running identical scripts in the same session)
    script_hash = hashlib.md5(script.strip().encode()).hexdigest()
    if script_hash in ctx.deps.script_output_cache:
        logger.info(
            "Script cache hit (hash %s), skipping sandbox execution.", script_hash
        )
        return (
            ctx.deps.script_output_cache[script_hash]
            + "\n\n[CACHE HIT — result from earlier identical script]"
        )

    # Create the cache serialization block safely
    serialized_cache = json.dumps(ctx.deps.sql_query_cache)

    # Trace context propagation (Strategy 4 - Observability)
    import os

    traceparent = os.getenv("TRACEPARENT", "")

    # Inject into the sandbox environment without asyncio to comply with sandbox rules
    augmented_prelude = (
        _GUARDRAIL_PREAMBLE
        + f"""
# Pre-populate query cache securely from the host
_PRE_POPULATED_CACHE = {serialized_cache}

# Trace parent context tag for telemetry
_TRACEPARENT = "{traceparent}"

# recurse handler: non-functional inside sandbox due to boundary, mock securely
def recurse(context, query, max_tokens=2000):
    return "[ERROR: recurse() is not supported inside sandbox due to MCP boundary. Use task_delegate tool instead.]"
"""
    )

    async with cl.Step(name="run_python_code", type="tool") as step:
        step.input = script
        if ctx.deps.add_step_callback:
            await ctx.deps.add_step_callback(
                {"tool": "run_python_code", "detail": "executing investigation script"}
            )
        try:
            # We append the user script to the prelude
            # Safe cache dump appended at script exit (to bypass atexit sandbox restrictions)
            cache_dump_suffix = "\n\n# Safe cache dump\ntry:\n    import json\n    print('<cache-delta>' + json.dumps(strake._cache) + '</cache-delta>')\nexcept Exception:\n    pass\n"
            guarded_script = augmented_prelude + "\n" + script + cache_dump_suffix

            result = await _call_mcp_tool(ctx, "run_python", {"script": guarded_script})
            texts = []
            for content in getattr(result, "content", []):
                if hasattr(content, "text"):
                    texts.append(content.text)
                elif isinstance(content, dict) and "text" in content:
                    texts.append(str(content["text"]))
                else:
                    texts.append(str(content))

            output = "\n".join(texts) or "(empty result)"

            # Safe Stream Serialization Parser: Extract and update sql_query_cache
            cache_match = re.search(
                r"<cache-delta>(.*?)</cache-delta>", output, re.DOTALL
            )
            if cache_match:
                try:
                    new_cache = json.loads(cache_match.group(1).strip())
                    ctx.deps.sql_query_cache.update(new_cache)
                except Exception as cache_err:
                    logger.error(
                        "Failed to parse cache delta from sandbox: %s", cache_err
                    )
                # Strip cache delta block from output completely
                output = re.sub(
                    r"<cache-delta>.*?</cache-delta>", "", output, flags=re.DOTALL
                ).strip()

            if len(output) < 100 and (
                "error" in output.lower()
                or "exception" in output.lower()
                or "traceback" in output.lower()
            ):
                logger.error(
                    "Guardrail or script startup error (short output): %r", output
                )
            if len(output) > MAX_TOOL_OUTPUT_CHARS:
                output = (
                    output[:MAX_TOOL_OUTPUT_CHARS]
                    + f"\n\n[TRUNCATED — {len(output):,} chars total, {len(output) - MAX_TOOL_OUTPUT_CHARS:,} dropped]"
                    + "\n[If you are seeing this repeatedly: STOP narrowing the query.]"
                    + "\n[Instead: fetch the full table once, filter in Python, print a summary.]"
                )

            step.output = output
            if len(output) <= SCRIPT_SHORT_OUTPUT_WARN_CHARS:
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

            # Store raw output in cross-turn cache before feedback is appended
            ctx.deps.script_output_cache[script_hash] = output

            # Append hints to what the model reads
            if quality_hints:
                hint_block = "\n\n--- AGENT FEEDBACK ---\n" + "\n".join(quality_hints)
                output = output + hint_block

            if call_n == SCRIPT_EFFICIENCY_WARNING_CALL:
                output += (
                    "\n\n--- EFFICIENCY WARNING ---\n"
                    f"This is sandbox call #{call_n} this turn. "
                    "If you have not yet done a multi-source Python analysis, do it now in a single script. "
                    "Fetch all needed data in one script, join in Python, and produce a final summary."
                )
            elif call_n >= SCRIPT_EFFICIENCY_CRITICAL_CALL:
                output += (
                    "\n\n--- EFFICIENCY CRITICAL ---\n"
                    f"This is sandbox call #{call_n} this turn. "
                    "STOP issuing individual queries. Write ONE final script that fetches everything "
                    "you still need, joins it in Python, and produces your root-cause summary."
                )

            res = output
        except Exception as e:
            step.output = f"Error: {e}"
            logger.error("Script execution failed: %s", e)
            res = f"Execution error: {e}"
    return res


@agent.tool
@child_agent.tool
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
            result = await _call_mcp_tool(ctx, "search_schemas", {"query": keyword})
            texts = []
            for content in getattr(result, "content", []):
                if hasattr(content, "text"):
                    texts.append(content.text)
                else:
                    texts.append(str(content))
            output = "\n".join(texts) or "No matching schemas."

            # Post-process to remove 'description: null' lines for cleaner AI context
            if output:
                lines = [
                    line
                    for line in output.splitlines()
                    if "description: null" not in line.lower()
                ]
                output = "\n".join(lines).strip() or "No matching schemas."

            step.output = output
            logger.info("Schema search complete.")
            res = output
        except Exception as e:
            step.output = f"Error: {e}"
            logger.error("Schema search failed: %s", e)
            res = f"Schema search error: {e}"
    return res


@agent.tool
async def task_propose(ctx: RunContext[Deps], items: list[dict]) -> str:
    """Propose a list of tasks for the investigation plan.

    This handles the user confirmation flow. If approved, all tasks are
    created in the TaskManager.

    Args:
        ctx: The run context.
        items: List of search/investigation steps: {subject: str, description: str, blocked_by: list[str]}.

    Returns:
        Success message or rejection from user.
    """
    if not ctx.deps.tasks:
        return "Error: TaskManager not initialized."

    async with cl.Step(name="task_propose", type="tool") as step:
        ctx.deps.rounds_since_todo = 0

        # Render markdown for confirmation
        lines = ["| **Task** | **Description** | **Prerequisite** |", "|---|---|---|"]
        for item in items:
            deps = ", ".join(item.get("blocked_by", [])) or "None"
            desc = item.get("description", "")
            lines.append(f"| {item['subject']} | {desc} | {deps} |")

        rendered = "\n".join(lines)
        step.input = json.dumps(items, indent=2)
        step.output = rendered

        if ctx.deps.todo_confirmation_callback:
            if ctx.deps.set_investigating_callback:
                await ctx.deps.set_investigating_callback(False, None)

            logger.info("Waiting for user confirmation of investigation plan.")
            confirmed = await ctx.deps.todo_confirmation_callback(rendered)

            if not confirmed:
                logger.info("User rejected the plan.")
                return "User REJECTED this plan. Please refine your strategy, ask for clarification, or try a simpler approach."

            if ctx.deps.set_investigating_callback:
                await ctx.deps.set_investigating_callback(True, None)
            logger.info("User confirmed the plan.")

        # Create tasks in TaskManager
        # Pass 1: Create all tasks to get their IDs
        subject_to_id = {}
        for item in items:
            res_str = ctx.deps.tasks.create(
                item["subject"],
                item.get("description", ""),
                blocked_by=[],  # Dependencies handled in pass 2
            )
            res = json.loads(res_str)
            subject_to_id[item["subject"]] = res["id"]

        # Pass 2: Link dependencies
        for item in items:
            target_id = subject_to_id[item["subject"]]
            for blocked_by_subject in item.get("blocked_by", []):
                if blocked_by_subject in subject_to_id:
                    ctx.deps.tasks.link(target_id, subject_to_id[blocked_by_subject])
                else:
                    # If it's not in our new batch, it might be an existing ID?
                    # Try to link it directly (TaskManager will validate)
                    ctx.deps.tasks.link(target_id, blocked_by_subject)

        summary = [f"Plan approved. {len(subject_to_id)} tasks created and linked:"]
        for subj, tid in subject_to_id.items():
            summary.append(f"  - {tid}: {subj}")

        return "\n".join(summary)


@agent.tool
async def task_delegate(ctx: RunContext[Deps], prompt: str) -> str:
    """Delegate a self-contained subtask to a subagent with a fresh context.

    USE THIS when a subtask requires 3+ sandbox script executions on its own
    (e.g. 'find all affected pods', 'correlate error rates across all services',
    'look up on-call contacts for every impacted service').
    The subagent runs independently and returns a summary.
    DO NOT use for quick single-query lookups — do those inline.
    "### When to Delegate\n"
    "Use `task_delegate` when a task step needs 3 or more script executions on its own. "
    "Examples that SHOULD be delegated:\n"
    "  • 'Find all pods with error_rate > threshold and their recent deploys'\n"
    "  • 'Cross-reference every alert with its on-call owner and last deployer'\n"
    "Examples that should NOT be delegated (do inline):\n"
    "  • A single metric lookup\n"
    "  • Fetching alerts for a known time window\n\n"
    """
    recursion_depth = getattr(ctx.deps, "recursion_depth", 0)
    max_recursion_depth = getattr(ctx.deps, "max_recursion_depth", 5)

    if recursion_depth >= max_recursion_depth:
        return f"Error: MAX RECURSION DEPTH {max_recursion_depth} REACHED."

    logger.info("Subagent spawned for prompt: %60s...", prompt[:60])

    # Create fresh deps for the child, but share the core state
    child_deps = Deps(
        mcp_session=ctx.deps.mcp_session,
        mcp_tools=ctx.deps.mcp_tools,
        rounds_since_todo=0,
        used_sources=ctx.deps.used_sources,
        todo_confirmation_callback=ctx.deps.todo_confirmation_callback,
        set_investigating_callback=ctx.deps.set_investigating_callback,
        add_step_callback=ctx.deps.add_step_callback,
        tasks=ctx.deps.tasks,
        background=ctx.deps.background,
        is_child=True,
        recursion_depth=recursion_depth + 1,
        max_recursion_depth=max_recursion_depth,
    )

    async with cl.Step(name="task_delegate", type="tool") as step:
        step.input = prompt
        try:
            logger.info("Subagent START: %s", str(prompt)[:50])
            result = await child_agent.run(prompt, deps=child_deps)

            # Sync usage back to parent
            ctx.deps.cumulative_usage += result.usage()

            summary = result.data
            step.output = summary
            logger.info("Subagent finished.")
            return summary
        except Exception as e:
            logger.error("Subagent failed: %s", e)
            return f"Subagent error: {e}"


@agent.tool
@child_agent.tool
async def load_skill(_ctx: RunContext[Deps], name: str) -> str:
    """Load a named skill runbook for reference during an investigation.

    Skills contain proven investigation patterns that can help prevent
    common mistakes. Use them for guidance if you encounter a complex
    incident or tricky code patterns.

    Args:
        _ctx: The run context.
        name: Name of the skill to load.

    Returns:
        The content of the skill or an error message.
    """
    logger.info("Loading skill: %s", name)
    return SKILL_LOADER.load(name)


@agent.tool
@child_agent.tool
async def task_create(
    ctx: RunContext[Deps],
    subject: str,
    description: str = "",
    blocked_by: Optional[list[str]] = None,
) -> str:
    """Create a new task. Optionally block it on existing tasks.

    Args:
        ctx: The run context.
        subject: Short label for the task.
        description: Extended detail.
        blocked_by: IDs of tasks that must complete first.
    """
    if not ctx.deps.tasks:
        return "Error: TaskManager not initialized."
    return ctx.deps.tasks.create(subject, description, blocked_by)


@agent.tool
async def task_link(ctx: RunContext[Deps], task_id: str, blocked_by_id: str) -> str:
    """Add a dependency between two existing tasks.

    Args:
        ctx: The run context.
        task_id: The task to be blocked.
        blocked_by_id: The task it must wait for.
    """
    if not ctx.deps.tasks:
        return "Error: TaskManager not initialized."
    return ctx.deps.tasks.link(task_id, blocked_by_id)


@agent.tool
@child_agent.tool
async def task_update(
    ctx: RunContext[Deps],
    task_id: str,
    status: Optional[str] = None,
    owner: Optional[str] = None,
) -> str:
    """Update task status and/or owner. Completing a task automatically unblocks dependents.

    Args:
        ctx: The run context.
        task_id: ID of the task to update.
        status: One of: pending, in_progress, completed.
        owner: Teammate name claiming the task.
    """
    if not ctx.deps.tasks:
        return "Error: TaskManager not initialized."
    return ctx.deps.tasks.update(task_id, status, owner)


@agent.tool
@child_agent.tool
async def task_list(ctx: RunContext[Deps]) -> str:
    """List all tasks with their status and dependency state."""
    if not ctx.deps.tasks:
        return "Error: TaskManager not initialized."
    return ctx.deps.tasks.list_all()


@agent.tool
@child_agent.tool
async def task_list_ready(ctx: RunContext[Deps]) -> str:
    """List only tasks that are pending with no remaining blockers — safe to start immediately."""
    if not ctx.deps.tasks:
        return "Error: TaskManager not initialized."
    return ctx.deps.tasks.list_ready()


@agent.tool
@child_agent.tool
async def task_get(ctx: RunContext[Deps], task_id: str) -> str:
    """Get full details for a single task including derived dependency fields."""
    if not ctx.deps.tasks:
        return "Error: TaskManager not initialized."
    return ctx.deps.tasks.get(task_id)


@agent.tool
@child_agent.tool
async def background_run(ctx: RunContext[Deps], command: str) -> str:
    """Run an allowlisted long-running shell command in the background.

    Returns immediately with a task ID. The result will be injected into
    the conversation once the command completes.
    """
    if not ctx.deps.background:
        return "Error: BackgroundManager not initialized."
    return ctx.deps.background.run(command)


@agent.tool
@child_agent.tool
async def background_status(ctx: RunContext[Deps], task_id: str) -> str:
    """Check the current status and result of a background task."""
    if not ctx.deps.background:
        return "Error: BackgroundManager not initialized."
    status = ctx.deps.background.get_status(task_id)
    if not status:
        return f"Error: Task {task_id} not found."
    return json.dumps(status, indent=2)


@agent.tool
@child_agent.tool
async def background_list(ctx: RunContext[Deps]) -> str:
    """List all background tasks with their current status."""
    if not ctx.deps.background:
        return "Error: BackgroundManager not initialized."
    tasks = ctx.deps.background.list_all()
    return json.dumps(tasks, indent=2)


@agent.tool
async def review_result(
    _ctx: RunContext[Deps],
    query: str,
    result: str,
    confidence: Literal["high", "medium", "low"],
) -> str:
    """
    Submit a result for review before finalizing. Mandatory before finalize().

    For LOW confidence: Returns suggestions for additional checks.
    For HIGH confidence: Approves and allows finalization.
    """
    if confidence == "low":
        return (
            "LOW CONFIDENCE flagged. Before finalizing:\n"
            "1. Cross-check with a second data source\n"
            "2. Verify row counts match expectations\n"
            "3. Run a sanity check query\n"
            "Call review_result again with findings."
        )

    return "Review passed. You may now call finalize()."


@agent.tool
async def compact(_ctx: RunContext[Deps]) -> str:
    """Manually compress the conversation history when context is getting long.

    Returns:
        A sentinel string to trigger compaction in the application layer.
    """
    return "__COMPACT_REQUESTED__"


@agent.tool
async def finalize(ctx: RunContext[Deps], answer: str) -> str:
    """Signal that the investigation is complete with a final answer.

    Call this when you have synthesized all recursive results into
    a conclusive root-cause analysis. This terminates the RLM loop.
    """
    return f"__FINALIZED__\n{answer}"
