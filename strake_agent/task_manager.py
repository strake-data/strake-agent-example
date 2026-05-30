import json
import uuid
import threading
import os
from pathlib import Path
from typing import Optional
from .config import logger


class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # guards file scan + write sequences

    # ── Public API ──────────────────────────────────────────────────────────

    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: Optional[list[str]] = None,
    ) -> str:
        blocked_by = blocked_by or []
        with self._lock:
            # Validate all prerequisite IDs exist before writing
            for dep_id in blocked_by:
                self._load(dep_id)  # raises FileNotFoundError if missing

            task = {
                "id": uuid.uuid4().hex[:8],
                "subject": subject,
                "description": description,
                "status": "pending",
                "blockedBy": blocked_by,
                "owner": "",
                "worktree": "",
            }
            self._save(task)
            logger.info("Created task %s: %s", task["id"], subject)
        return json.dumps(self._enrich(task, self._build_blocks_map()), indent=2)

    def link(self, task_id: str, blocked_by_id: str) -> str:
        with self._lock:
            if self._has_cycle(task_id, blocked_by_id):
                return (
                    f"Error: linking {task_id} → {blocked_by_id} would create a cycle"
                )
            task = self._load(task_id)
            if blocked_by_id not in task["blockedBy"]:
                task["blockedBy"].append(blocked_by_id)
                self._save(task)
                logger.info("Linked task %s to blocker %s", task_id, blocked_by_id)
        return json.dumps(self._enrich(task, self._build_blocks_map()), indent=2)

    def update(
        self, task_id: str, status: Optional[str] = None, owner: Optional[str] = None
    ) -> str:

        VALID_STATUSES = {"pending", "in_progress", "completed"}

        with self._lock:
            task = self._load(task_id)

            if status is not None:
                if status not in VALID_STATUSES:
                    return f"Error: invalid status '{status}'"
                task["status"] = status

            if owner is not None:
                task["owner"] = owner

            self._save(task)
            if status == "completed":
                self._resolve(task_id)
            logger.info("Updated task %s: status=%s, owner=%s", task_id, status, owner)
        return json.dumps(self._enrich(task, self._build_blocks_map()), indent=2)

    def get(self, task_id: str) -> str:
        with self._lock:
            task = self._load(task_id)
            return json.dumps(self._enrich(task, self._build_blocks_map()), indent=2)

    def list_all(self) -> str:
        with self._lock:
            tasks = self._load_all_tasks()
            blocks_map = self._build_blocks_map(tasks)
            enriched = [self._enrich(t, blocks_map) for t in tasks]
            return json.dumps(enriched, indent=2)

    def list_ready(self) -> str:
        with self._lock:
            tasks = self._load_all_tasks()
            blocks_map = self._build_blocks_map(tasks)
            ready = [
                self._enrich(t, blocks_map)
                for t in tasks
                if t["status"] == "pending" and not t["blockedBy"]
            ]
            return json.dumps(ready, indent=2)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _load(self, task_id: str) -> dict:
        target = self.dir / f"task_{task_id}.json"
        if not target.exists():
            raise FileNotFoundError(f"Task {task_id} not found")
        return json.loads(target.read_text())

    def _save(self, task: dict) -> None:
        target = self.dir / f"task_{task['id']}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(task, indent=2))
        os.replace(tmp, target)  # atomic on POSIX and Windows

    def _resolve(self, completed_id: str) -> None:
        # Called inside self._lock — no re-acquire
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def _load_all_tasks(self) -> list[dict]:
        return [json.loads(f.read_text()) for f in sorted(self.dir.glob("task_*.json"))]

    def _build_blocks_map(
        self, tasks: Optional[list[dict]] = None
    ) -> dict[str, list[str]]:
        tasks = tasks or self._load_all_tasks()
        blocks_map = {task["id"]: [] for task in tasks}
        for task in tasks:
            for dep_id in task.get("blockedBy", []):
                blocks_map.setdefault(dep_id, []).append(task["id"])
        return blocks_map

    def _has_cycle(self, from_id: str, to_id: str) -> bool:
        """Return True if adding edge from_id -> to_id (from_id blocked by to_id)
        would introduce a cycle. Traverses blockedBy edges from to_id upward."""
        visited = set()
        stack = [to_id]
        while stack:
            node = stack.pop()
            if node == from_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            try:
                task = self._load(node)
                stack.extend(task.get("blockedBy", []))
            except FileNotFoundError:
                pass
        return False

    def _enrich(
        self,
        task: dict,
        blocks_map: Optional[dict[str, list[str]]] = None,
    ) -> dict:
        enriched = dict(task)
        enriched["isReady"] = (
            task["status"] == "pending" and len(task["blockedBy"]) == 0
        )
        enriched["isBlocked"] = len(task["blockedBy"]) > 0
        blocks_map = blocks_map or self._build_blocks_map()
        enriched["blocks"] = blocks_map.get(task["id"], [])
        return enriched
