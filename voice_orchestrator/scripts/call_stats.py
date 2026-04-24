"""
call_stats_service.py

Computes per-agent statistics from CallLog records.

Usage:
    from call_stats_service import compute_agent_stats

    stats = compute_agent_stats(
        queryset=CallLog.objects.filter(campaign=my_campaign),
    )
    # Returns a list of dicts, one per agent.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from itertools import pairwise

from django.db.models import QuerySet
from dialer.models import CallLog


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHIFT_START_HOUR = 6   # 06:00 UTC
SHIFT_END_HOUR   = 14  # 14:00 UTC  (i.e. 14:00 is the deadline)
LOG_OUT_GAP_THRESHOLD = timedelta(minutes=5)
MIN_UNIQUE_PHONE_CALLS_PER_DAY = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shift_boundaries(date):
    """Return (shift_start, shift_end) as UTC-aware datetimes for a given date."""
    tz = timezone.utc
    start = datetime.combine(date, time(SHIFT_START_HOUR, 0, 0), tzinfo=tz)
    end   = datetime.combine(date, time(SHIFT_END_HOUR,   0, 0), tzinfo=tz)
    return start, end


def _to_date(dt):
    """Convert an aware datetime to its UTC date."""
    return dt.astimezone(timezone.utc).date()


# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

def compute_agent_stats(queryset: QuerySet) -> list[dict]:
    """
    Compute per-agent statistics from a CallLog queryset.

    Parameters
    ----------
    queryset : QuerySet[CallLog]
        Pre-filtered CallLog records. Select-related / prefetch as needed;
        this function only reads Python-level attributes.

    Returns
    -------
    list[dict]  — one dict per agent, with keys:
        agent_id, agent_name,
        avg_late_minutes,
        avg_calls_per_day,
        avg_answered_per_day,
        avg_not_answered_per_day,
        avg_busy_per_day,
        avg_logged_out_minutes_per_day,
        days_active
    """

    # Pull only the fields we need from the DB in one shot.
    records = (
        queryset
        .select_related("agent", "lead")
        .filter(initiated_at__isnull=False, agent_id__in=[4, 5])
        .values(
            "agent_id",
            "agent__user__first_name",          # adjust to your Agent str field
            "initiated_at",
            "status",
            "lead__phone_number",
        )
        .order_by("agent_id", "initiated_at")
    )

    # Group by agent
    by_agent: dict[int, list[dict]] = defaultdict(list)
    agent_names: dict[int, str] = {}

    for row in records:
        aid = row["agent_id"]
        if aid is None:
            continue
        by_agent[aid].append(row)
        agent_names[aid] = row["agent__user__first_name"] or f"Agent {aid}"

    results = []

    for agent_id, calls in by_agent.items():
        # Sort just in case (already ordered by DB, but be safe)
        calls.sort(key=lambda r: r["initiated_at"])

        # Group calls by UTC date
        by_day: dict = defaultdict(list)
        for call in calls:
            d = _to_date(call["initiated_at"])
            by_day[d].append(call)

        # Per-day accumulators
        late_offsets        = []   # minutes; negative = late arrival
        calls_per_day       = []
        answered_per_day    = []
        not_answered_per_day= []
        busy_per_day        = []
        logged_out_per_day  = []   # minutes

        for date, day_calls in by_day.items():
            shift_start, shift_end = _shift_boundaries(date)

            # --- Unique phone numbers dialled ----------------------------
            unique_phones = {c["lead__phone_number"] for c in day_calls if c["lead__phone_number"]}
            if len(unique_phones) < MIN_UNIQUE_PHONE_CALLS_PER_DAY:
                continue

            calls_per_day.append(len(unique_phones))

            # --- Status breakdown ----------------------------------------
            statuses = [c["status"] for c in day_calls]
            answered_per_day.append(statuses.count("answered"))
            not_answered_per_day.append(statuses.count("no_answer"))
            busy_per_day.append(statuses.count("busy"))

            # --- Lateness ------------------------------------------------
            # First call offset relative to shift start (negative = late)
            first_call_time = day_calls[0]["initiated_at"]
            late_delta = (shift_start - first_call_time).total_seconds() / 60.0
            # If first_call_time > shift_start  →  late_delta is negative  →  agent was late
            # If first_call_time < shift_start  →  late_delta is positive  →  agent was early
            # We store as "minutes late", so positive = late, negative = early:
            late_minutes = -late_delta   # flip sign: + means agent started late
            late_offsets.append(late_minutes)

            # --- Time logged out -----------------------------------------
            # Walk consecutive pairs of initiated_at timestamps.
            # A gap > 5 min counts as logged-out time, UNLESS the earlier
            # call in the pair had status == 'answered' (because the call
            # itself may still be in progress / wrap-up).
            logged_out_seconds = 0.0
            for prev, curr in pairwise(day_calls):
                gap = (curr["initiated_at"] - prev["initiated_at"]).total_seconds()
                if gap > LOG_OUT_GAP_THRESHOLD.total_seconds():
                    if prev["status"] != "answered":
                        logged_out_seconds += gap

            logged_out_per_day.append(logged_out_seconds / 60.0)  # → minutes

        # --- Aggregate across days -------------------------------------------
        days = len(calls_per_day)

        if not days:
            continue

        def _avg(lst):
            return sum(lst) / len(lst) if lst else 0.0

        results.append({
            "agent_id":                     agent_id,
            "agent_name":                   agent_names[agent_id],
            "days_active":                  days,
            # Positive = agent starts late; negative = agent starts early
            "avg_late_minutes":             round(_avg(late_offsets), 2),
            "avg_calls_per_day":            round(_avg(calls_per_day), 2),
            "avg_answered_per_day":         round(_avg(answered_per_day), 2),
            "avg_not_answered_per_day":     round(_avg(not_answered_per_day), 2),
            "avg_busy_per_day":             round(_avg(busy_per_day), 2),
            "avg_logged_out_minutes_per_day": round(_avg(logged_out_per_day), 2),
        })

    results.sort(key=lambda r: r["agent_name"])
    return results

from datetime import date
start = date(2026, 4, 14)

stats = compute_agent_stats(
    CallLog.objects.filter(initiated_at__date__gte=start)
)
