"""Module for compressing conversation history to manage context window."""

import datetime
import json
import time
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from .config import (
    AUTO_COMPACT_THRESHOLD,
    INTRA_RUN_TOOL_RESULT_CAP,
    KEEP_RECENT_FULL,
    MAX_CONVERSATION_CHARS,
    MIN_COMPRESSIBLE_LEN,
    TOOL_RESULT_PREVIEW_LEN,
    get_model,
    logger,
)

TRANSCRIPT_DIR = Path(".transcripts")
TRANSCRIPT_DIR.mkdir(exist_ok=True)


def estimate_tokens(messages: list[ModelMessage]) -> int:
    """Return the actual context window size based on the most recent API call.

    The last ModelResponse.usage.input_tokens tells you exactly how many tokens
    Gemini received on the most recent round — that IS your current context size.
    Earlier responses are not summed; their input_tokens are already subsets of
    the latest round's count.

    Args:
        messages: The full conversation history.

    Returns:
        Current context size in tokens, or a character-based fallback.
    """
    for msg in reversed(messages):
        if isinstance(msg, ModelResponse) and msg.usage is not None:
            # input_tokens = full context sent; output_tokens = response generated
            return msg.usage.input_tokens + msg.usage.output_tokens
    # Fallback: only synthetic injected messages, no real API calls yet
    return sum(len(str(m)) // 3 for m in messages)


def micro_compact(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Replace old tool results with placeholders to save space.

    Args:
        messages: List of model messages.

    Returns:
        The compacted list of messages.
    """
    tool_return_indices: list[tuple[int, int]] = []

    for i, msg in enumerate(messages):
        if isinstance(msg, ModelRequest):
            for j, part in enumerate(msg.parts):
                if isinstance(part, ToolReturnPart):
                    tool_return_indices.append((i, j))

    if len(tool_return_indices) <= KEEP_RECENT_FULL:
        return messages

    to_replace = tool_return_indices[:-KEEP_RECENT_FULL]
    for msg_idx, part_idx in to_replace:
        msg = messages[msg_idx]
        if isinstance(msg, ModelRequest):
            part = msg.parts[part_idx]
            if (
                isinstance(part, ToolReturnPart)
                and len(str(part.content)) > MIN_COMPRESSIBLE_LEN
            ):
                new_parts = list(msg.parts)
                new_parts[part_idx] = ToolReturnPart(
                    tool_name=part.tool_name,
                    content=f"[Previous: used {part.tool_name}]",
                    tool_call_id=part.tool_call_id,
                    timestamp=part.timestamp,
                )
                messages[msg_idx] = ModelRequest(parts=new_parts)
    return messages


summarize_agent = Agent(
    get_model("summarizer"),
    instructions=(
        "You are a summarization assistant. Given a conversation between a user "
        "and a DevOps incident response agent (including tool calls and results), "
        "produce a dense summary preserving: the incident being investigated, "
        "findings so far, queries run, key facts discovered, and next steps. "
        "Write as a continuation briefing for the agent."
    ),
)


def _save_transcript(messages: list[ModelMessage]) -> Path:
    """Save the full conversation transcript to a JSON file.

    Args:
        messages: List of model messages.

    Returns:
        Path to the saved transcript file.
    """
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.json"
    serialized = [
        m.model_dump() if hasattr(m, "model_dump") else str(m) for m in messages
    ]
    path.write_text(json.dumps(serialized, indent=2, default=str))
    logger.info("Transcript saved to %s", path)
    return path


async def auto_compact(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Summarize the conversation and replace history with the summary.

    Args:
        messages: List of model messages.

    Returns:
        A list of messages containing only the summary and a confirmation.
    """
    transcript_path = _save_transcript(messages)

    conversation_text = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart):
                    conversation_text.append(f"USER: {part.content}")
                elif isinstance(part, ToolReturnPart):
                    conversation_text.append(
                        f"TOOL_RESULT ({part.tool_name}): {str(part.content)[:TOOL_RESULT_PREVIEW_LEN]}"
                    )
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    conversation_text.append(f"ASSISTANT: {part.content}")

    conversation_str = "\n\n".join(conversation_text)[:MAX_CONVERSATION_CHARS]

    logger.info("Starting auto-compaction summarization.")
    summary_result = await summarize_agent.run(
        f"Summarize this incident response session for continuity:\n\n{conversation_str}"
    )
    summary = summary_result.output

    compressed: list[ModelMessage] = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=f"[Session compressed — transcript saved to {transcript_path}]\n\n{summary}"
                )
            ]
        ),
        ModelResponse(
            parts=[
                TextPart(
                    content="Understood. I have the context summary and am ready to continue the investigation."
                )
            ],
            # Use a generic model name or the name from the result
            model_name=getattr(summary_result, "model_name", "model"),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        ),
    ]
    logger.info("Auto-compaction complete.")
    return compressed


def intra_run_compact(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Truncate old tool results within an active run before each round.

    Keeps the KEEP_RECENT_FULL most recent ToolReturnParts at full length.
    Older ones are capped at INTRA_RUN_TOOL_RESULT_CAP characters.
    Tool call / return pairing is always preserved.

    Args:
        messages: Full history including current-run messages.

    Returns:
        Modified history safe to send to the model.
    """
    if messages:
        for msg in reversed(messages):
            if isinstance(msg, ModelResponse) and msg.usage is not None:
                logger.debug(
                    "intra_run_compact: round context = %d input + %d output tokens",
                    msg.usage.input_tokens,
                    msg.usage.output_tokens,
                )
                break

    # Collect all ToolReturnPart positions in order
    positions: list[tuple[int, int]] = []  # (message_idx, part_idx)
    for i, msg in enumerate(messages):
        if isinstance(msg, ModelRequest):
            for j, part in enumerate(msg.parts):
                if isinstance(part, ToolReturnPart):
                    positions.append((i, j))

    # Only truncate the older ones — leave the newest KEEP_RECENT_FULL intact
    to_truncate = (
        positions[:-KEEP_RECENT_FULL] if len(positions) > KEEP_RECENT_FULL else []
    )

    if not to_truncate:
        return messages

    # Work on a copy to avoid side effects during the run
    messages = list(messages)

    for msg_idx, part_idx in to_truncate:
        msg = messages[msg_idx]
        if not isinstance(msg, ModelRequest):
            continue
        part = msg.parts[part_idx]
        if not isinstance(part, ToolReturnPart):
            continue

        content_str = str(part.content)
        if len(content_str) <= INTRA_RUN_TOOL_RESULT_CAP:
            continue  # already short, don't touch it

        truncated = content_str[:INTRA_RUN_TOOL_RESULT_CAP]
        new_parts = list(msg.parts)
        new_parts[part_idx] = ToolReturnPart(
            tool_name=part.tool_name,
            content=f"{truncated}\n…[truncated, {len(content_str)} chars total]",
            tool_call_id=part.tool_call_id,
            timestamp=part.timestamp,
        )
        messages[msg_idx] = ModelRequest(parts=tuple(new_parts))

    return messages
