"""What the AI tutor is doing, what the class misunderstands, and how one
student got where they are.

Three lecturer views, all built from the events stream:

- tutor_activity      — help-ladder usage, and the patterns worth questioning
- misconception_radar — the same wrong belief recurring across students
- student_timeline    — one student's significant moments, in order

Pure functions over event rows so each rule is unit-tested without a
database; the endpoints in app/api/v1/lecturer/analytics.py only fetch.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from app.core.mastery import band

# A student asking for the full solution this often in the window is named.
FREQUENT_REQUESTS = 5
# A misconception is on the radar once this many students share it.
RADAR_MIN_STUDENTS = 2
EMERGING_DAYS = 7


def _level(e: dict):
    level = (e.get("payload") or {}).get("help_level")
    return level if isinstance(level, int) and 0 <= level <= 5 else None


def tutor_activity(turns: list[dict]) -> dict:
    """Help-ladder counts and questionable patterns over ``turn.completed`` rows.

    A full solution the student did not ask for, with no hint earlier in the
    session, is the tutor skipping the ladder — the thing a lecturer most
    needs to know the AI is doing on their behalf.
    """
    counts = Counter()
    requests: Counter = Counter()
    by_session: dict[str, list[dict]] = defaultdict(list)
    for e in turns:
        level = _level(e)
        if level is None:
            continue
        counts[level] += 1
        if level == 5 and (e.get("payload") or {}).get("help_asked"):
            requests[e.get("user_id")] += 1
        by_session[e.get("session_id") or ""].append(e)

    skipped_ladder = 0
    for session_turns in by_session.values():
        session_turns.sort(key=lambda e: e.get("created_at") or "")
        hinted = False
        for e in session_turns:
            level = _level(e)
            if level == 5 and not (e.get("payload") or {}).get("help_asked") and not hinted:
                skipped_ladder += 1
                break
            if level is not None and 1 <= level <= 4:
                hinted = True

    return {
        "counts": {
            "concept_explanations": counts[0],
            "socratic_prompts": counts[1],
            "concept_cues": counts[2],
            "structural_hints": counts[3],
            "partial_steps": counts[4],
            "worked_solutions": counts[5],
            "solutions_requested": sum(requests.values()),
        },
        "sessions_skipping_ladder": skipped_ladder,
        "frequent_requesters": [
            {"student_id": sid, "requests": n}
            for sid, n in requests.most_common()
            if sid and n >= FREQUENT_REQUESTS
        ],
    }


def misconception_radar(detections: list[dict], now: datetime) -> dict:
    """Group ``misconception.detected`` rows by topic and label.

    Labels are grouped case-insensitively; the grader is shown the labels
    already in use so the same error is named the same way. Only errors shared
    by several students are a class matter — one student's error belongs on
    their timeline.
    """
    groups: dict[tuple[str, str], dict] = {}
    for e in detections:
        label = ((e.get("payload") or {}).get("label") or "").strip()
        if not label or not e.get("user_id"):
            continue
        key = ((e.get("topic") or "").casefold(), label.casefold())
        g = groups.setdefault(key, {
            "topic": e.get("topic") or None, "spellings": Counter(),
            "students": set(), "occurrences": 0, "first_seen": None, "last_seen": None,
        })
        g["spellings"][label] += 1
        g["students"].add(e["user_id"])
        g["occurrences"] += 1
        ts = e.get("created_at") or ""
        g["first_seen"] = min(filter(None, [g["first_seen"], ts]), default=None)
        g["last_seen"] = max(filter(None, [g["last_seen"], ts]), default=None)

    recent = (now - timedelta(days=EMERGING_DAYS)).isoformat()
    shared = [
        {
            "topic": g["topic"],
            "label": g["spellings"].most_common(1)[0][0],
            "students": len(g["students"]),
            "student_ids": sorted(g["students"]),
            "occurrences": g["occurrences"],
            "first_seen": g["first_seen"],
            "last_seen": g["last_seen"],
            # New this week: the moment to address it in a lecture is now.
            "emerging": bool(g["first_seen"]) and g["first_seen"] >= recent,
        }
        for g in groups.values()
        if len(g["students"]) >= RADAR_MIN_STUDENTS
    ]
    shared.sort(key=lambda m: (-m["students"], not m["emerging"], m["label"]))
    return {
        "misconceptions": shared,
        "individual": sum(1 for g in groups.values() if len(g["students"]) < RADAR_MIN_STUDENTS),
    }


def student_timeline(events: list[dict]) -> list[dict]:
    """One student's significant moments, oldest first.

    Mastery is reported as band changes — mastered, slipped, recovered — not
    as every attempt; a Learn session as one line summarising the help it
    took. Each entry: ``{at, kind, text, topic?}``.
    """
    out: list[dict] = []
    bands: dict[str, str] = {}
    slipped: set[str] = set()
    sessions: dict[str, dict] = {}

    for e in sorted(events, key=lambda e: e.get("created_at") or ""):
        kind, at, topic = e.get("event_type"), e.get("created_at"), e.get("topic")
        payload = e.get("payload") or {}

        if kind == "mastery.updated" and topic and isinstance(payload.get("score"), (int, float)):
            now_band = band(float(payload["score"]))
            before = bands.get(topic)
            bands[topic] = now_band
            pct = f"{round(float(payload['score']) * 100)}%"
            source = "an exam" if payload.get("source") == "exam" else "Review"
            if before is None:
                out.append({"at": at, "kind": "started", "topic": topic,
                            "text": f"First graded attempt on {topic} ({source}): {pct}."})
            elif now_band == "secure" and before != "secure":
                if topic in slipped:
                    slipped.discard(topic)
                    out.append({"at": at, "kind": "recovered", "topic": topic,
                                "text": f"Recovered {topic}: back to secure at {pct}."})
                else:
                    out.append({"at": at, "kind": "mastered", "topic": topic,
                                "text": f"Mastered {topic}: secure at {pct} ({source})."})
            elif before == "secure" and now_band != "secure":
                slipped.add(topic)
                out.append({"at": at, "kind": "slipped", "topic": topic,
                            "text": f"Slipped on {topic}: down to {pct} after being secure."})
            elif now_band == "struggling" and before != "struggling":
                out.append({"at": at, "kind": "struggling", "topic": topic,
                            "text": f"Struggling with {topic}: {pct}."})

        elif kind == "misconception.detected" and payload.get("label"):
            where = f" ({topic})" if topic else ""
            out.append({"at": at, "kind": "misconception", "topic": topic,
                        "text": f"Misconception{where}: {payload['label']}."})

        elif kind == "turn.completed":
            level = _level(e)
            if level is None or level == 0:
                continue
            sid = e.get("session_id") or at
            s = sessions.get(sid)
            if s is None:
                s = {"at": at, "kind": "tutoring", "hints": 0, "solutions": 0, "asked": 0}
                sessions[sid] = s
                out.append(s)
            if level == 5:
                s["solutions"] += 1
                s["asked"] += 1 if payload.get("help_asked") else 0
            else:
                s["hints"] += 1

        elif kind == "assessment.submitted":
            out.append({"at": at, "kind": "exam",
                        "text": f"Submitted an exam ({payload.get('answered', '?')} answers)."})

    for entry in out:
        if entry["kind"] == "tutoring":
            parts = []
            if entry["hints"]:
                parts.append(f"{entry['hints']} hint{'s' if entry['hints'] != 1 else ''}")
            if entry["solutions"]:
                asked = f", {entry['asked']} asked for" if entry["asked"] else ""
                parts.append(f"{entry['solutions']} full solution{'s' if entry['solutions'] != 1 else ''}{asked}")
            entry["text"] = "Worked problems with the tutor: " + " and ".join(parts) + "."
            for k in ("hints", "solutions", "asked"):
                entry.pop(k)
    return out
