"""
Todoist integration for Mickey Marathon.

Uses the unified Todoist API v1 via the official `todoist-api-python` SDK
(v4.x, httpx-based). The personal API token lives in Secret Manager
(mickey-marathon-todoist-token); project and label IDs are resolved by
name once per process and cached.

Every workout task Mickey creates goes in the project
"Goal 4: Run a 4h Marathon" with the label
"Have and Project a Youthful Energy" — that's the contract with
Jonathan's Todoist setup, hard-coded here on purpose.
"""
import logging
import os
from typing import Any, Dict, List, Optional

from todoist_api_python.api import TodoistAPI

from .secret_utilities import get_secret_from_secret_manager

logger = logging.getLogger(__name__)

TODOIST_TOKEN_SECRET_ID = "mickey-marathon-todoist-token"
WORKOUT_PROJECT_NAME = "Goal 4: Run a 4h Marathon"
WORKOUT_LABEL_NAME = "Have and Project a Youthful Energy"

_api: Optional[TodoistAPI] = None
_project_id: Optional[str] = None


def _secret_project() -> str:
    return os.environ.get("AGENT_PROJECT_ID", "mickey-marathon")


def _get_api() -> TodoistAPI:
    global _api
    if _api is None:
        token = get_secret_from_secret_manager(_secret_project(), TODOIST_TOKEN_SECRET_ID)
        _api = TodoistAPI(token)
    return _api


def _get_project_id() -> str:
    """Resolve the workout project by name (cached per process)."""
    global _project_id
    if _project_id is None:
        api = _get_api()
        for page in api.get_projects():
            for project in page:
                if project.name.strip().lower() == WORKOUT_PROJECT_NAME.lower():
                    _project_id = project.id
                    break
            if _project_id:
                break
        if _project_id is None:
            raise ValueError(
                f"Todoist project {WORKOUT_PROJECT_NAME!r} not found. Create it "
                f"(or fix WORKOUT_PROJECT_NAME in todoist_utilities.py) and retry."
            )
    return _project_id


def create_workout_task(content: str, description: str = "", due_date: str = "") -> Dict[str, Any]:
    """Create a workout task in the marathon project with the standard label.

    Args:
        content: Task title, e.g. "Run: 6mi with 3x9min tempo".
        description: Full workout detail (exercises/weights for lifting days).
        due_date: YYYY-MM-DD; defaults to today if empty.

    Returns:
        {task_id, url} for later completion.
    """
    api = _get_api()
    task = api.add_task(
        content=content,
        description=description or None,
        project_id=_get_project_id(),
        labels=[WORKOUT_LABEL_NAME],
        due_string=due_date or "today",
    )
    return {"task_id": task.id, "url": getattr(task, "url", None)}


def complete_workout_task(task_id: str) -> Dict[str, Any]:
    """Mark a workout task complete."""
    api = _get_api()
    ok = api.complete_task(task_id)
    return {"task_id": task_id, "completed": bool(ok)}


def find_open_workout_tasks() -> List[Dict[str, Any]]:
    """List open tasks in the marathon project (for finding today's task
    when the memory doc's task-id map is missing or stale)."""
    api = _get_api()
    out = []
    for page in api.get_tasks(project_id=_get_project_id()):
        for task in page:
            out.append({
                "task_id": task.id,
                "content": task.content,
                "due": task.due.date if task.due else None,
                "labels": task.labels,
            })
    return out
