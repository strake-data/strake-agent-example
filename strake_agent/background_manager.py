import subprocess
import threading
import uuid
import shlex
from pathlib import Path
from typing import Dict, List, Optional


class BackgroundManager:
    """Manages long-running shell commands in background daemon threads."""

    _ALLOWED_PREFIXES: tuple[tuple[str, ...], ...] = (
        ("cargo",),
        ("pytest",),
        ("uv", "run"),
        ("python",),
        ("python3",),
        ("bash",),
        ("sh",),
        ("git", "status"),
        ("git", "diff"),
        ("ls",),
        ("rg",),
        ("cat",),
        ("sed",),
        ("tail",),
        ("head",),
        ("sleep",),
        ("echo",),
    )
    _SHELL_METACHARS = {"|", "&", ";", ">", "<", "$", "`", "(", ")"}

    def __init__(self, workdir: Path = Path.cwd()):
        self.workdir = workdir
        self.tasks: Dict[str, dict] = {}
        self._notification_queue: List[dict] = []
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        """Spawn a command in a background thread and return its task ID."""
        try:
            argv = self._validate_command(command)
        except ValueError as exc:
            return f"Error: {exc}"

        task_id = str(uuid.uuid4())[:8]
        with self._lock:
            self.tasks[task_id] = {
                "id": task_id,
                "command": command,
                "status": "running",
                "result": None,
            }

        thread = threading.Thread(
            target=self._execute, args=(task_id, command, argv), daemon=True
        )
        thread.start()
        return f"Background task {task_id} started: `{command}`"

    def _execute(self, task_id: str, command: str, argv: list[str]):
        """Standard thread target for background execution."""
        try:
            # Run the command with a 5-minute timeout
            process = subprocess.run(
                argv,
                shell=False,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = (process.stdout + process.stderr).strip()
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
        except Exception as e:
            output = f"Error: {e}"

        with self._lock:
            self.tasks[task_id]["status"] = "completed"
            self.tasks[task_id]["result"] = output
            # Add to notification queue for the agent loop to pick up
            self._notification_queue.append(
                {
                    "task_id": task_id,
                    "command": command,
                    "result": output[:1000],  # Truncate for the notification
                }
            )

    def drain_notifications(self) -> List[dict]:
        """Return and clear the current notification queue."""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
            return notifs

    def get_status(self, task_id: str) -> Optional[dict]:
        """Get the current status of a background task."""
        with self._lock:
            return self.tasks.get(task_id)

    def list_all(self) -> List[dict]:
        """List all background tasks and their statuses."""
        with self._lock:
            return list(self.tasks.values())

    def _validate_command(self, command: str) -> list[str]:
        stripped: str = command.strip()
        if not stripped:
            raise ValueError("background command cannot be empty")
        if any(char in stripped for char in self._SHELL_METACHARS):
            raise ValueError(
                "shell metacharacters are not allowed in background commands"
            )

        argv: List[str] = shlex.split(stripped)
        if not argv:
            raise ValueError("background command cannot be empty")

        for prefix in self._ALLOWED_PREFIXES:
            if tuple(argv[: len(prefix)]) == prefix:
                return argv

        raise ValueError(
            "background command is not allowlisted; permitted prefixes include "
            + ", ".join(" ".join(prefix) for prefix in self._ALLOWED_PREFIXES)
        )
