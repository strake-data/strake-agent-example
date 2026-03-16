"""Data models for managing the investigation todo list."""

from enum import StrEnum
from pydantic import BaseModel, model_validator


class TodoStatus(StrEnum):
    """Enum for possible todo item statuses."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class TodoItem(BaseModel):
    """Represents a single task in an investigation.

    Attributes:
        id: Unique identifier for the task.
        text: Description of the task.
        status: The current status of the task.
    """

    id: str
    text: str
    status: TodoStatus = TodoStatus.PENDING


class TodoList(BaseModel):
    """A collection of todo items with validation and rendering logic.

    Attributes:
        items: List of todo items.
    """

    items: list[TodoItem] = []

    @model_validator(mode="after")
    def single_in_progress(self) -> "TodoList":
        """Ensure only one task is in_progress at any given time.

        Raises:
            ValueError: If more than one task is in_progress.
        """
        in_prog = [i for i in self.items if i.status == TodoStatus.IN_PROGRESS]
        if len(in_prog) > 1:
            raise ValueError("Only one task can be in_progress at a time")
        return self

    def render(self, *, show_prompt: bool = False) -> str:
        """Render the todo list as a markdown table.

        Args:
            show_prompt: Whether to append a confirmation prompt.

        Returns:
            The markdown-formatted todo list.
        """
        if not self.items:
            return "No todos yet."

        done = sum(1 for i in self.items if i.status == TodoStatus.DONE)
        total = len(self.items)

        status_label = {
            TodoStatus.PENDING: "Pending",
            TodoStatus.IN_PROGRESS: "In Progress",
            TodoStatus.DONE: "Done",
        }

        lines = [
            "| # | Task | Status |",
            "|---|------|--------|",
        ]
        for idx, item in enumerate(self.items, 1):
            lines.append(f"| {idx} | {item.text} | {status_label[item.status]} |")

        lines.append(f"\n**Progress: {done}/{total}**")

        if show_prompt:
            lines.append("\n**Do you want to proceed with this plan?**")

        return "\n".join(lines)
