"""
Garmin Connect integration for Mickey Marathon.

All Garmin reads/writes go through the unofficial `garminconnect` library
(v0.3.x, curl_cffi-based client). The deployed agent NEVER logs in with
credentials — Garmin blocks headless credential logins (Cloudflare TLS
fingerprinting, March 2026). Instead it loads a token bundle (long-lived
OAuth1 ~1 year + auto-refreshing OAuth2) from Secret Manager, bootstrapped
interactively by scripts/bootstrap_garmin_tokens.py.

When the bundle finally expires or is revoked, every function here raises
GarminAuthExpired. The agent prompt translates that into a message telling
Jonathan to re-run the bootstrap script — do not retry, do not attempt a
credential login.

Times/dates: Garmin's connectapi works in the user's local calendar days.
All "today" logic here uses America/New_York.
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from garminconnect import Garmin
from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    RunningWorkout,
    StepType,
    WorkoutSegment,
)

from .secret_utilities import get_secret_from_secret_manager

logger = logging.getLogger(__name__)

HOME_TZ = ZoneInfo("America/New_York")
ML_PER_OUNCE = 29.5735

GARMIN_TOKENS_SECRET_ID = "mickey-marathon-garmin-tokens"

# Names of workouts Mickey pushes carry this prefix so she can find and
# remove exactly her own workouts later without touching ones Jonathan
# created himself in Garmin Connect.
WORKOUT_NAME_PREFIX = "Mickey: "


class GarminAuthExpired(Exception):
    """The stored Garmin token bundle no longer works.

    Recovery is ALWAYS manual: Jonathan runs
    scripts/bootstrap_garmin_tokens.py on a workstation, which rotates the
    bundle in Secret Manager. The agent must surface this plainly and not
    retry.
    """


_client: Optional[Garmin] = None
_loaded_bundle: Optional[str] = None


def _secret_project() -> str:
    return os.environ.get("AGENT_PROJECT_ID", "mickey-marathon")


def _today() -> str:
    return datetime.now(HOME_TZ).strftime("%Y-%m-%d")


def _get_client() -> Garmin:
    """Load the token bundle from Secret Manager and log in token-only.

    Cached per process (cold-start cost paid on the first Garmin call).
    On success, if the client refreshed the OAuth2 token during load, the
    refreshed bundle is written back to Secret Manager (best-effort) so a
    container restart doesn't redo the refresh dance.
    """
    global _client, _loaded_bundle
    if _client is not None:
        return _client

    try:
        bundle = get_secret_from_secret_manager(_secret_project(), GARMIN_TOKENS_SECRET_ID)
    except Exception as e:
        raise GarminAuthExpired(
            f"Could not read the Garmin token bundle from Secret Manager "
            f"({GARMIN_TOKENS_SECRET_ID}): {e}. Jonathan needs to run "
            f"scripts/bootstrap_garmin_tokens.py to (re)provision it."
        ) from e

    try:
        client = Garmin()
        client.login(tokenstore=bundle)
        # Cheap authenticated call to prove the tokens actually work.
        client.get_full_name()
    except Exception as e:
        raise GarminAuthExpired(
            f"Garmin token login failed ({type(e).__name__}: {e}). The stored "
            f"token bundle has likely expired — Jonathan needs to re-run "
            f"scripts/bootstrap_garmin_tokens.py on his workstation."
        ) from e

    _client = client
    _loaded_bundle = bundle
    _persist_refreshed_tokens()
    return _client


def _persist_refreshed_tokens() -> None:
    """Write the current token bundle back to Secret Manager if it changed."""
    global _loaded_bundle
    if _client is None:
        return
    try:
        current = _client.client.dumps()
        if current and current != _loaded_bundle:
            from google.cloud import secretmanager

            sm = secretmanager.SecretManagerServiceClient()
            parent = f"projects/{_secret_project()}/secrets/{GARMIN_TOKENS_SECRET_ID}"
            sm.add_secret_version(
                request={"parent": parent, "payload": {"data": current.encode("utf-8")}}
            )
            _loaded_bundle = current
            logger.info("Persisted refreshed Garmin token bundle to Secret Manager")
    except Exception as e:
        # Best-effort: a failed write-back only costs a re-refresh next cold start.
        logger.warning(f"Could not persist refreshed Garmin tokens: {e}")


def _auth_guard(e: Exception) -> None:
    """Re-raise auth-shaped errors as GarminAuthExpired."""
    msg = str(e).lower()
    if "401" in msg or "unauthorized" in msg or "authentication" in msg:
        raise GarminAuthExpired(
            f"Garmin rejected the stored tokens mid-session ({e}). Jonathan "
            f"needs to re-run scripts/bootstrap_garmin_tokens.py."
        ) from e


# ============================================================================
# Hydration
# ============================================================================

def log_water_ounces(ounces: float, date: Optional[str] = None) -> Dict[str, Any]:
    """Log water consumed, in fluid ounces, to the given date (YYYY-MM-DD,
    defaults to today). Returns that day's new totals."""
    resolved_date = date or _today()
    client = _get_client()
    try:
        client.add_hydration_data(value_in_ml=round(ounces * ML_PER_OUNCE, 1), cdate=resolved_date)
        return get_hydration_today(resolved_date)
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise


def get_hydration_today(date: Optional[str] = None) -> Dict[str, Any]:
    """Hydration for the given date (YYYY-MM-DD, defaults to today):
    consumed vs goal, in both ml and ounces."""
    resolved_date = date or _today()
    client = _get_client()
    try:
        data = client.get_hydration_data(resolved_date) or {}
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    consumed_ml = data.get("valueInML") or 0
    goal_ml = data.get("goalInML") or 0
    return {
        "date": resolved_date,
        "consumed_ml": consumed_ml,
        "consumed_oz": round(consumed_ml / ML_PER_OUNCE, 1),
        "goal_ml": goal_ml,
        "goal_oz": round(goal_ml / ML_PER_OUNCE, 1) if goal_ml else None,
    }


# ============================================================================
# Alarms / readiness / vitals
# ============================================================================

def get_alarms() -> List[Dict[str, Any]]:
    """Enabled alarms across every Garmin device on the account.

    Returns a list of {device, time (HH:MM 24h, device-local), days, enabled}.
    """
    client = _get_client()
    try:
        raw = client.get_device_alarms() or []
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    alarms = []
    for a in raw:
        # alarmTime is minutes past midnight in most payloads.
        minutes = a.get("alarmTime")
        if isinstance(minutes, int) and minutes <= 24 * 60:
            time_str = f"{minutes // 60:02d}:{minutes % 60:02d}"
        else:
            time_str = str(minutes)
        alarms.append({
            "device": a.get("deviceName") or a.get("deviceId"),
            "time": time_str,
            "days": a.get("alarmDays") or a.get("alarmDayOfWeek") or [],
            "enabled": (a.get("alarmMode") or a.get("alarmStatus", "")).upper() not in ("OFF", "DISABLED", ""),
            "raw_mode": a.get("alarmMode") or a.get("alarmStatus"),
        })
    return alarms


def get_readiness_snapshot() -> Dict[str, Any]:
    """This morning's readiness picture: sleep, HRV, RHR, body battery,
    Garmin training readiness. Individual sections may be None if the
    device didn't record them."""
    client = _get_client()
    today = _today()
    out: Dict[str, Any] = {"date": today}
    try:
        sleep = client.get_sleep_data(today) or {}
        daily = sleep.get("dailySleepDTO") or {}
        out["sleep"] = {
            "bedtime_gmt": daily.get("sleepStartTimestampGMT"),
            "wake_gmt": daily.get("sleepEndTimestampGMT"),
            "duration_hours": round((daily.get("sleepTimeSeconds") or 0) / 3600, 2),
            "sleep_score": (daily.get("sleepScores") or {}).get("overall", {}).get("value"),
        }
        hrv = client.get_hrv_data(today) or {}
        out["hrv"] = {
            "last_night_avg": (hrv.get("hrvSummary") or {}).get("lastNightAvg"),
            "status": (hrv.get("hrvSummary") or {}).get("status"),
        }
        rhr = client.get_rhr_day(today) or {}
        metrics = (rhr.get("allMetrics") or {}).get("metricsMap") or {}
        rhr_values = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
        out["resting_hr"] = rhr_values[0].get("value") if rhr_values else None
        bb = client.get_body_battery(today) or []
        out["body_battery"] = {
            "charged": bb[0].get("charged") if bb else None,
            "drained": bb[0].get("drained") if bb else None,
            "current": (bb[0].get("bodyBatteryValuesArray") or [[None, None]])[-1][1] if bb else None,
        }
        tr = client.get_training_readiness(today) or []
        if isinstance(tr, list) and tr:
            out["training_readiness"] = {
                "score": tr[0].get("score"),
                "level": tr[0].get("level"),
                "feedback": tr[0].get("feedbackShort"),
            }
        else:
            out["training_readiness"] = None
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    return out


def get_calories_burned_today() -> Dict[str, Any]:
    """Today's calories: total, active, and BMR."""
    client = _get_client()
    try:
        stats = client.get_stats(_today()) or {}
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    return {
        "date": _today(),
        "total_kcal": stats.get("totalKilocalories"),
        "active_kcal": stats.get("activeKilocalories"),
        "bmr_kcal": stats.get("bmrKilocalories"),
    }


# ============================================================================
# Activities
# ============================================================================

def get_recent_activities(days: int = 14) -> List[Dict[str, Any]]:
    """Activities from the last `days` days, newest first. Each entry:
    activity_id, name, type, start_local, distance_miles, duration_minutes,
    calories, avg_hr, start_lat, start_lon."""
    client = _get_client()
    start = (datetime.now(HOME_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        raw = client.get_activities_by_date(start, _today()) or []
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    out = []
    for a in raw:
        out.append({
            "activity_id": a.get("activityId"),
            "name": a.get("activityName"),
            "type": (a.get("activityType") or {}).get("typeKey"),
            "start_local": a.get("startTimeLocal"),
            "distance_miles": round((a.get("distance") or 0) / 1609.344, 2),
            "duration_minutes": round((a.get("duration") or 0) / 60, 1),
            "calories": a.get("calories"),
            "avg_hr": a.get("averageHR"),
            "start_lat": a.get("startLatitude"),
            "start_lon": a.get("startLongitude"),
        })
    return out


def get_activity_detail(activity_id: str) -> Dict[str, Any]:
    """Full detail for one activity: summary + per-split paces + gear."""
    client = _get_client()
    try:
        splits = client.get_activity_splits(str(activity_id)) or {}
        gear = client.get_activity_gear(str(activity_id)) or []
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    lap_summaries = []
    for lap in (splits.get("lapDTOs") or []):
        lap_summaries.append({
            "lap": lap.get("lapIndex"),
            "distance_miles": round((lap.get("distance") or 0) / 1609.344, 2),
            "duration_minutes": round((lap.get("duration") or 0) / 60, 2),
            "avg_hr": lap.get("averageHR"),
            "avg_pace_min_per_mile": (
                round((lap.get("duration") or 0) / 60 / ((lap.get("distance") or 1) / 1609.344), 2)
                if lap.get("distance") else None
            ),
        })
    return {
        "activity_id": activity_id,
        "splits": lap_summaries,
        "gear": [
            {"name": g.get("customMakeModel") or g.get("displayName"), "uuid": g.get("uuid")}
            for g in gear
        ],
    }


# ============================================================================
# Workouts (push to watch / remove)
# ============================================================================

_STEP_TYPES = {
    "warmup": {"stepTypeId": StepType.WARMUP, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": StepType.COOLDOWN, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": StepType.INTERVAL, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": StepType.RECOVERY, "stepTypeKey": "recovery", "displayOrder": 4},
}

_TIME_CONDITION = {
    "conditionTypeId": ConditionType.TIME,
    "conditionTypeKey": "time",
    "displayOrder": 2,
    "displayable": True,
}
_DISTANCE_CONDITION = {
    "conditionTypeId": ConditionType.DISTANCE,
    "conditionTypeKey": "distance",
    "displayOrder": 1,
    "displayable": True,
}
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}

_RUNNING_SPORT = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}


def push_run_workout_to_watch(name: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a structured running workout, upload it, and schedule it for
    today so it syncs to the watch.

    Each step dict: {"step_type": "warmup"|"interval"|"recovery"|"cooldown",
    and EITHER "duration_minutes": float OR "distance_miles": float}.
    Returns {workout_id, name, scheduled_date}.
    """
    workout_steps: List[ExecutableStep] = []
    total_secs = 0
    for i, s in enumerate(steps, start=1):
        step_type = _STEP_TYPES.get(str(s.get("step_type", "interval")).lower(), _STEP_TYPES["interval"])
        if s.get("distance_miles"):
            end_condition = _DISTANCE_CONDITION
            end_value = float(s["distance_miles"]) * 1609.344  # meters
            total_secs += int(float(s["distance_miles"]) * 10 * 60)  # rough 10min/mile estimate
        else:
            end_condition = _TIME_CONDITION
            end_value = float(s.get("duration_minutes", 10)) * 60
            total_secs += int(end_value)
        workout_steps.append(ExecutableStep(
            stepOrder=i,
            stepType=step_type,
            endCondition=end_condition,
            endConditionValue=end_value,
            targetType=_NO_TARGET,
        ))

    full_name = name if name.startswith(WORKOUT_NAME_PREFIX) else f"{WORKOUT_NAME_PREFIX}{name}"
    workout = RunningWorkout(
        workoutName=full_name,
        estimatedDurationInSecs=total_secs,
        workoutSegments=[WorkoutSegment(
            segmentOrder=1,
            sportType=_RUNNING_SPORT,
            workoutSteps=workout_steps,
        )],
    )

    client = _get_client()
    try:
        created = client.upload_running_workout(workout)
        workout_id = created.get("workoutId")
        client.schedule_workout(workout_id, _today())
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    return {"workout_id": workout_id, "name": full_name, "scheduled_date": _today()}


def push_strength_workout_to_watch(name: str, duration_minutes: int) -> Dict[str, Any]:
    """Push a simple timed strength workout to the watch for today.

    Garmin's planned-workout API cannot carry exercise names/weights, so
    the watch gets a single timed strength block; the exact exercises,
    sets, and target weights belong in the Todoist task and the message
    to Jonathan.
    """
    full_name = name if name.startswith(WORKOUT_NAME_PREFIX) else f"{WORKOUT_NAME_PREFIX}{name}"
    workout_json = {
        "workoutName": full_name,
        "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
        "estimatedDurationInSecs": duration_minutes * 60,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
            "workoutSteps": [{
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": {"stepTypeId": StepType.INTERVAL, "stepTypeKey": "interval", "displayOrder": 3},
                "endCondition": _TIME_CONDITION,
                "endConditionValue": duration_minutes * 60,
                "targetType": _NO_TARGET,
            }],
        }],
    }
    client = _get_client()
    try:
        created = client.upload_workout(workout_json)
        workout_id = created.get("workoutId")
        client.schedule_workout(workout_id, _today())
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    return {"workout_id": workout_id, "name": full_name, "scheduled_date": _today()}


def remove_workout_from_watch(workout_name: str) -> Dict[str, Any]:
    """Delete Mickey-pushed workout(s) matching `workout_name` (with or
    without the 'Mickey: ' prefix). Only touches workouts whose names
    start with the prefix. Returns the deleted workout ids."""
    client = _get_client()
    wanted = workout_name if workout_name.startswith(WORKOUT_NAME_PREFIX) else f"{WORKOUT_NAME_PREFIX}{workout_name}"
    deleted = []
    try:
        for w in client.get_workouts() or []:
            w_name = w.get("workoutName") or ""
            if w_name == wanted or (
                w_name.startswith(WORKOUT_NAME_PREFIX) and workout_name.lower() in w_name.lower()
            ):
                client.delete_workout(w.get("workoutId"))
                deleted.append({"workout_id": w.get("workoutId"), "name": w_name})
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    return {"deleted": deleted}


# ============================================================================
# Body composition
# ============================================================================

def get_body_comp_last_week() -> List[Dict[str, Any]]:
    """Last 7 days of weigh-ins: [{date, weight_lbs, body_fat_pct}], most
    recent first. Days with no weigh-in are omitted."""
    client = _get_client()
    start = (datetime.now(HOME_TZ) - timedelta(days=6)).strftime("%Y-%m-%d")
    try:
        data = client.get_body_composition(start, _today()) or {}
    except GarminAuthExpired:
        raise
    except Exception as e:
        _auth_guard(e)
        raise
    entries = []
    for e_ in (data.get("dateWeightList") or []):
        weight_g = e_.get("weight")
        entries.append({
            "date": e_.get("calendarDate"),
            "weight_lbs": round(weight_g / 453.592, 1) if weight_g else None,
            "body_fat_pct": e_.get("bodyFat"),
        })
    entries.sort(key=lambda x: x["date"] or "", reverse=True)
    return entries
