"""
Custom function tools for Mickey Marathon.

Each function here is wrapped in `google.adk.tools.FunctionTool` in
agent.py. Docstrings are the tool contracts the LLM sees — keep them
precise about arguments, units, and return shapes.

Garmin functions raise GarminAuthExpired (garmin_utilities) when the
stored token bundle stops working; the error text tells the model what to
say (Jonathan must re-run scripts/bootstrap_garmin_tokens.py). Do not
retry those.
"""
import functools
import logging
import os
from typing import Any, Dict, List

from . import garmin_utilities
from . import todoist_utilities
from .docs_utilities import get_docs_connector
from .sheets_utilities import get_sheets_connector

logger = logging.getLogger(__name__)

PLAN_SHEET_ID_ENV = "MARATHON_PLAN_SHEET_ID"
PLAN_TAB = "Current Marathon Plan"
PHILOSOPHY_TAB = "Philosophy"


def _tool(func):
    """Return errors to the model instead of raising.

    A raised exception aborts the whole turn on Agent Engine (the user just
    sees an empty reply), so every tool converts failures into
    {"error": "..."} results the model can read, explain, and act on —
    which is exactly what the prompt's Garmin-auth-failure protocol needs.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Tool {func.__name__} failed: {type(e).__name__}: {e}")
            return {"error": f"{type(e).__name__}: {e}"}
    return wrapper


def _plan_sheet_id() -> str:
    sheet_id = os.environ.get(PLAN_SHEET_ID_ENV)
    if not sheet_id:
        raise ValueError(
            f"{PLAN_SHEET_ID_ENV} is not set — the deployment is missing the "
            f"training-plan spreadsheet ID in .env."
        )
    return sheet_id


# ============================================================================
# Persistent memory (Google Doc) — template pattern, unchanged
# ============================================================================

@_tool
def get_agent_memory() -> str:
    """
    Retrieve Mickey's persistent memory from the configured Google Doc.

    The memory doc holds: the pupil profile & goals, equipment defaults by
    location (plus the travel default), processed-activity IDs, the
    Todoist task map, and current race metadata. Read it before any task
    that needs that state.

    Returns:
        The full text content of the memory document (may be empty on
        first ever use).
    """
    doc_id = os.environ.get("AGENT_MEMORY_DOC_ID")
    if not doc_id:
        raise ValueError("AGENT_MEMORY_DOC_ID is not set in the environment.")
    return get_docs_connector().read_doc(doc_id)


@_tool
def update_agent_memory(updated_memory: str) -> Dict[str, Any]:
    """
    Replace Mickey's persistent memory with the provided text.

    Args:
        updated_memory: COMPLETE new memory document text (this replaces
            the whole document — never pass a fragment).

    Returns:
        API response confirming the update.
    """
    doc_id = os.environ.get("AGENT_MEMORY_DOC_ID")
    if not doc_id:
        raise ValueError("AGENT_MEMORY_DOC_ID is not set in the environment.")
    return get_docs_connector().write_doc(doc_id, updated_memory)


# ============================================================================
# Garmin: hydration
# ============================================================================

@_tool
def log_water(ounces: float) -> Dict[str, Any]:
    """
    Log water Jonathan just drank, in fluid ounces, to Garmin Connect.

    Args:
        ounces: Amount of water in fluid ounces (e.g. 20).

    Returns:
        Today's updated hydration totals: {date, consumed_ml, consumed_oz,
        goal_ml, goal_oz}.
    """
    return garmin_utilities.log_water_ounces(ounces)


@_tool
def get_hydration_today() -> Dict[str, Any]:
    """
    Get today's hydration status from Garmin Connect.

    Returns:
        {date, consumed_ml, consumed_oz, goal_ml, goal_oz} — goal fields
        may be None if no goal is configured.
    """
    return garmin_utilities.get_hydration_today()


# ============================================================================
# Garmin: alarms / readiness / calories
# ============================================================================

@_tool
def get_alarms() -> List[Dict[str, Any]]:
    """
    Get alarm settings from EVERY Garmin device on Jonathan's account
    (watch and sleep monitors — the alarm may live on any of them).

    Returns:
        List of {device, time (HH:MM 24h), days, enabled, raw_mode}.
        Consider only entries with enabled=True when determining when
        Jonathan plans to wake up.
    """
    return garmin_utilities.get_alarms()


@_tool
def get_readiness_snapshot() -> Dict[str, Any]:
    """
    Get this morning's readiness picture from Garmin: last night's sleep
    (bedtime, duration, score), HRV status, resting HR, body battery, and
    Garmin's own training-readiness score.

    Returns:
        {date, sleep: {...}, hrv: {...}, resting_hr, body_battery: {...},
        training_readiness: {score, level, feedback} | None}. Sections may
        be None when the device didn't record them.
    """
    return garmin_utilities.get_readiness_snapshot()


@_tool
def get_calories_burned_today() -> Dict[str, Any]:
    """
    Get today's calories burned from Garmin.

    Returns:
        {date, total_kcal, active_kcal, bmr_kcal}.
    """
    return garmin_utilities.get_calories_burned_today()


# ============================================================================
# Garmin: activities
# ============================================================================

@_tool
def get_recent_activities(days: int) -> List[Dict[str, Any]]:
    """
    Get Jonathan's Garmin activities from the last N days, newest first.

    Args:
        days: How many days back to look (e.g. 1 for the hourly workout
            check, 14 for the weekly equipment audit).

    Returns:
        List of {activity_id, name, type, start_local, distance_miles,
        duration_minutes, calories, avg_hr, start_lat, start_lon}.
    """
    return garmin_utilities.get_recent_activities(days)


@_tool
def get_activity_detail(activity_id: str) -> Dict[str, Any]:
    """
    Get full detail for one Garmin activity: per-lap splits (distance,
    duration, pace, HR) and the gear (shoes) logged on it.

    Args:
        activity_id: The activity_id from get_recent_activities.

    Returns:
        {activity_id, splits: [...], gear: [{name, uuid}]}.
    """
    return garmin_utilities.get_activity_detail(activity_id)


# ============================================================================
# Garmin: workouts on the watch
# ============================================================================

@_tool
def push_run_workout_to_watch(name: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build a structured running workout and schedule it on Jonathan's watch
    for TODAY. Steps become recognizable splits he can follow when he runs
    the workout from the watch.

    NEVER include a route/map — only durations or distances per segment.

    Args:
        name: Short workout name, e.g. "Tempo Tuesday" (auto-prefixed
            "Mickey: " so it can be found and removed later).
        steps: Ordered list of step dicts. Each has "step_type" (one of
            "warmup", "interval", "recovery", "cooldown") and EITHER
            "duration_minutes" (float) OR "distance_miles" (float).
            Example: [{"step_type": "warmup", "duration_minutes": 10},
                      {"step_type": "interval", "duration_minutes": 9},
                      {"step_type": "recovery", "duration_minutes": 3},
                      {"step_type": "cooldown", "duration_minutes": 10}]

    Returns:
        {workout_id, name, scheduled_date}.
    """
    return garmin_utilities.push_run_workout_to_watch(name, steps)


@_tool
def push_strength_workout_to_watch(name: str, duration_minutes: int) -> Dict[str, Any]:
    """
    Schedule a simple TIMED strength workout on the watch for today.

    Garmin's planned workouts cannot carry exercise names or weights — so
    the watch only gets a named, timed strength block. ALWAYS put the
    exact exercises, sets, reps, and target weights in the Todoist task
    description and in your message to Jonathan.

    Args:
        name: Short name, e.g. "Lower Body Strength".
        duration_minutes: Total planned duration.

    Returns:
        {workout_id, name, scheduled_date}.
    """
    return garmin_utilities.push_strength_workout_to_watch(name, duration_minutes)


@_tool
def remove_workout_from_watch(workout_name: str) -> Dict[str, Any]:
    """
    Delete a workout Mickey previously pushed to the watch (matched by
    name; only touches workouts named with the "Mickey: " prefix).

    Args:
        workout_name: The workout name used when pushing (with or without
            the "Mickey: " prefix).

    Returns:
        {deleted: [{workout_id, name}]} — empty list if nothing matched.
    """
    return garmin_utilities.remove_workout_from_watch(workout_name)


# ============================================================================
# Garmin: body composition
# ============================================================================

@_tool
def get_body_comp_last_week() -> List[Dict[str, Any]]:
    """
    Get the last 7 days of weight and body-fat measurements from Garmin,
    most recent first. Use this to answer the weight_body_fat_week inquiry
    (format the reply per the WEIGHT_BODY_FAT_REPORT spec in your
    instructions).

    Returns:
        List of {date, weight_lbs, body_fat_pct} — days without a weigh-in
        are omitted; body_fat_pct may be None.
    """
    return garmin_utilities.get_body_comp_last_week()


# ============================================================================
# Training-plan spreadsheet
# ============================================================================

@_tool
def read_philosophy() -> str:
    """
    Read the training-philosophy statement (Philosophy tab, cell A1) from
    the marathon-plan spreadsheet. ALWAYS read this before creating or
    modifying the training plan.

    Returns:
        The philosophy text (empty string if not yet written).
    """
    rows = get_sheets_connector().read_all(_plan_sheet_id(), f"'{PHILOSOPHY_TAB}'!A1")
    return rows[0][0] if rows and rows[0] else ""


@_tool
def write_philosophy(text: str) -> Dict[str, Any]:
    """
    Write the training-philosophy statement to Philosophy!A1 (replaces it).
    Do this only after talking the philosophy through with Jonathan.

    Args:
        text: The complete philosophy statement.
    """
    return get_sheets_connector().update_cells(
        _plan_sheet_id(), f"'{PHILOSOPHY_TAB}'!A1", [[text]]
    )


@_tool
def read_training_plan() -> List[Dict[str, Any]]:
    """
    Read the whole "Current Marathon Plan" tab.

    Layout contract: row 1 is the header (Week | Monday | ... | Sunday);
    column A of each following row is the Monday date (YYYY-MM-DD) of that
    week; columns B-H are Monday through Sunday. A day cell contains the
    planned workout text, and once the day is done also "Actual: ..." on
    a following line.

    Returns:
        List of {row: <1-based sheet row number>, values: [col A..H]} —
        use `row` to compute the A1 address of a day cell (Monday=B,
        Tuesday=C, ... Sunday=H).
    """
    rows = get_sheets_connector().read_all(_plan_sheet_id(), f"'{PLAN_TAB}'!A:H")
    return [{"row": i + 1, "values": r} for i, r in enumerate(rows)]


@_tool
def write_plan_cell(a1_cell: str, text: str) -> Dict[str, Any]:
    """
    Overwrite one cell of the "Current Marathon Plan" tab.

    Args:
        a1_cell: Cell address WITHOUT tab prefix, e.g. "C4" (row from
            read_training_plan, column B=Monday ... H=Sunday).
        text: New cell content. When recording a completed day, keep the
            plan and add the actual, e.g.:
            "Plan: 12mi long run\\nActual: 4mi easy".
    """
    return get_sheets_connector().update_cells(
        _plan_sheet_id(), f"'{PLAN_TAB}'!{a1_cell}", [[text]]
    )


@_tool
def write_plan_rows(start_row: int, rows: List[List[str]]) -> Dict[str, Any]:
    """
    Bulk-write plan rows starting at `start_row` (used when creating a new
    marathon plan). Each row is [week_monday_date, mon, tue, wed, thu,
    fri, sat, sun].

    Args:
        start_row: 1-based sheet row to start writing at (2 = first week
            row, below the header).
        rows: List of 8-element rows (col A through H).
    """
    end_row = start_row + len(rows) - 1
    return get_sheets_connector().update_cells(
        _plan_sheet_id(), f"'{PLAN_TAB}'!A{start_row}:H{end_row}", rows
    )


@_tool
def color_plan_cell(a1_cell: str, color: str) -> Dict[str, Any]:
    """
    Set the background color of a "Current Marathon Plan" day cell.

    Color code (the compliance convention):
      - "green":  did the planned workout, or something >=90% equivalent
      - "yellow": worked out, but not close to the plan
      - "red":    skipped the planned workout entirely
      - "none":   clear the background (planned, not yet done)

    Args:
        a1_cell: Cell address WITHOUT tab prefix, e.g. "C4".
        color: "green" | "yellow" | "red" | "none".
    """
    return get_sheets_connector().set_cell_background(
        _plan_sheet_id(), PLAN_TAB, a1_cell, color
    )


# ============================================================================
# Todoist
# ============================================================================

@_tool
def create_workout_task(content: str, description: str, due_date: str) -> Dict[str, Any]:
    """
    Create today's workout task in Todoist — always in the project
    "Goal 4: Run a 4h Marathon" with the label "Have and Project a
    Youthful Energy" (handled automatically).

    Args:
        content: Task title, e.g. "Run: 6mi — 10min easy, 3x(9min tempo /
            3min recovery), 10min cooldown".
        description: Full detail. For strength days: every exercise with
            sets, reps, and target weights.
        due_date: YYYY-MM-DD (empty string = today).

    Returns:
        {task_id, url}. SAVE task_id in the memory doc's Todoist task map
        so the post-workout flow can complete it.
    """
    return todoist_utilities.create_workout_task(content, description, due_date)


@_tool
def complete_workout_task(task_id: str) -> Dict[str, Any]:
    """
    Mark a Todoist workout task complete (post-workout flow).

    Args:
        task_id: The task id saved in the memory doc (or found via
            find_open_workout_tasks).

    Returns:
        {task_id, completed}.
    """
    return todoist_utilities.complete_workout_task(task_id)


@_tool
def find_open_workout_tasks() -> List[Dict[str, Any]]:
    """
    List open tasks in the marathon Todoist project. Use when the memory
    doc's task map doesn't have today's task id.

    Returns:
        List of {task_id, content, due, labels}.
    """
    return todoist_utilities.find_open_workout_tasks()
