"""Which students need the lecturer, and why.

Not a ranking by lowest grade. A low score on a topic the student started
yesterday is not news; these flags are the situations a lecturer can act on
and would not otherwise see:

- declining   — mastery falling despite continued practice
- dependency  — solving problems mostly by asking the tutor for the answer
- disengaged  — was working regularly, has stopped
- stuck       — still struggling on a topic after many attempts

Enrolled-but-never-started is deliberately absent: the Overview already has
its own banner for it, and early in a pilot it would bury everything else.

Every flag carries a sentence that says why, built only from recorded data.
Pure functions over rows the endpoint fetches, so the rules are unit-tested
without a database.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

# Priority order: what most needs a human first.
KINDS = ("declining", "dependency", "disengaged", "stuck")

DECLINE_WINDOW = 4          # consecutive mastery updates on one topic
DECLINE_DROP = 0.2          # net fall across that window
DEPENDENCY_MIN_PROBLEMS = 5
DEPENDENCY_SHARE = 0.6      # share of problems that ended in a full solution
IDLE_DAYS = 7
IDLE_MIN_SESSIONS = 3       # "was working regularly"
STUCK_MIN_ATTEMPTS = 5
STUCK_BELOW = 0.4


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def flags_for_course(
    *,
    now: datetime,
    enrolled: list[str],
    sessions: list[dict],
    mastery_events: list[dict],
    turn_events: list[dict],
    mastery_rows: list[dict],
) -> dict[str, list[dict]]:
    """``{student_id: [{"kind", "reason", "topic"?}, ...]}``, flagged students only."""
    enrolled_set = set(enrolled)
    out: dict[str, list[dict]] = defaultdict(list)

    # declining: per (student, topic), the last few updates in time order.
    series: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for e in mastery_events:
        score = (e.get("payload") or {}).get("score")
        if e.get("user_id") and e.get("topic") and isinstance(score, (int, float)):
            series[(e["user_id"], e["topic"])].append((e.get("created_at") or "", float(score)))
    for (sid, topic), points in series.items():
        points.sort()
        window = [s for _, s in points[-DECLINE_WINDOW:]]
        if len(window) == DECLINE_WINDOW and window[0] - window[-1] >= DECLINE_DROP:
            out[sid].append({
                "kind": "declining", "topic": topic,
                "reason": f"{topic}: mastery fell from {_pct(window[0])} to "
                          f"{_pct(window[-1])} over the last {DECLINE_WINDOW} attempts.",
            })

    # dependency: problem turns (help rung 1-5) and how many ended at rung 5.
    problems: dict[str, int] = defaultdict(int)
    solved_for: dict[str, int] = defaultdict(int)
    for e in turn_events:
        level = (e.get("payload") or {}).get("help_level")
        if e.get("user_id") and isinstance(level, int) and level >= 1:
            problems[e["user_id"]] += 1
            if level == 5:
                solved_for[e["user_id"]] += 1
    means: dict[str, list[float]] = defaultdict(list)
    for r in mastery_rows:
        means[r["student_id"]].append(float(r["mastery_score"]))
    for sid, n in problems.items():
        full = solved_for[sid]
        if n >= DEPENDENCY_MIN_PROBLEMS and full / n >= DEPENDENCY_SHARE:
            reason = f"Took the full worked solution on {full} of {n} problems in Learn"
            if means.get(sid):
                mean = sum(means[sid]) / len(means[sid])
                reason += f"; unaided mastery (Review and exams) is {_pct(mean)}"
            out[sid].append({"kind": "dependency", "reason": reason + "."})

    # disengaged, from sessions.
    per_student: dict[str, list[datetime]] = defaultdict(list)
    for s in sessions:
        started = _parse(s.get("started_at"))
        if s.get("user_id") and started:
            per_student[s["user_id"]].append(started)
    for sid in enrolled_set:
        starts = per_student.get(sid)
        if not starts:
            continue
        idle = (now - max(starts)).days
        if idle >= IDLE_DAYS and len(starts) >= IDLE_MIN_SESSIONS:
            out[sid].append({
                "kind": "disengaged",
                "reason": f"No activity for {idle} days, after {len(starts)} sessions before that.",
            })

    # stuck: many attempts, still in the struggling band.
    for r in mastery_rows:
        attempts = int(r.get("attempts_count") or 0)
        score = float(r["mastery_score"])
        if attempts >= STUCK_MIN_ATTEMPTS and score < STUCK_BELOW:
            already = any(f.get("topic") == r["topic"] for f in out[r["student_id"]])
            if not already:
                out[r["student_id"]].append({
                    "kind": "stuck", "topic": r["topic"],
                    "reason": f"{r['topic']}: still {_pct(score)} after {attempts} attempts.",
                })

    # Withdrawn students drop out of the list whatever their history says.
    return {
        sid: sorted(flags, key=lambda f: KINDS.index(f["kind"]))
        for sid, flags in out.items()
        if sid in enrolled_set and flags
    }
