"""The Socratic intervention ladder for Learn.

When a student brings a problem, the tutor does not solve it outright. It
climbs one rung at a time — a question about the goal, a concept cue, a
structural hint, the next step, and only then the worked solution — so the
student does the thinking the tutor would otherwise do for them.

The model reports the rung it used as a tag at the very start of its answer,
``[[help:3]]``. The tag is stripped from the stream before any client sees
it and recorded on the ``turn.completed`` event. That record is what lets a
lecturer see who is being handed full solutions, and what the next turn is
told about where the ladder stands.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

# Ladder rungs. 0 is "not a problem" — a concept question, taught normally.
LEVELS = {
    0: "concept explanation",
    1: "metacognitive prompt",
    2: "concept cue",
    3: "structural hint",
    4: "partial step",
    5: "worked solution",
}

LADDER_INSTRUCTION = """
SOCRATIC HELP LADDER (applies when the student brings a PROBLEM to solve — an
exercise, a homework or past-paper question, "find", "solve", "evaluate",
"differentiate", "prove" — not when they ask what a concept means):

Do NOT solve the student's problem straight away. Climb one rung per turn:
  1. Metacognitive prompt — ask what they are trying to find, or what they
     have tried. ("What is the question asking for?")
  2. Concept cue — name the idea that applies, without applying it.
     ("Which rule do we use to differentiate a product?")
  3. Structural hint — show how to set the problem up, not how to finish it.
     ("Write it as u times v and name u and v.")
  4. Partial step — work ONLY the next step, then ask them to continue.
  5. Worked solution — the full solution, every step explained.

- Start at rung 1 for a new problem. Go up ONE rung when the student is still
  stuck or asks for more help. If they make a real attempt, respond to their
  attempt: confirm what is right, point at the first thing that is wrong, and
  stay on (or drop to) the lowest rung that gets them moving again.
- If the student explicitly asks for the full solution or the answer, give it
  (rung 5). Do not argue or withhold; the lecturer sees how often this happens.
- Keep hint turns short. A rung-1 to rung-4 answer is a few sentences ending
  in one clear question, not a lecture — plain prose, NOT a slide deck, even
  when told to present on the lesson board. The board is for rungs 0 and 5.
- Worked examples YOU choose in order to explain a concept are not the
  student's problem; show those in full.

HELP TAG — begin EVERY answer with exactly one tag, before anything else:
  [[help:N]]        N = the rung you used, 1 to 5
  [[help:5:asked]]  the full solution, because the student asked for it
  [[help:0]]        not a problem at all — a concept explanation
The tag is removed before the student sees the answer. Never mention it.
"""

_TAG = re.compile(r"^\s*\[\[help:([0-5])(:asked)?\]\]\s*")
# The longest tag is "[[help:5:asked]]" — 16 characters, plus leading space.
_MAX_TAG = 24
_PREFIX = "[[help:"


def parse(text: str) -> tuple[Optional[int], bool, str]:
    """Split ``(level, asked, remaining_text)`` off the start of an answer."""
    m = _TAG.match(text or "")
    if not m:
        return None, False, text
    return int(m.group(1)), bool(m.group(2)), text[m.end():]


def previous_state_note(level: Optional[int]) -> str:
    """Tell the model where the ladder stood after its last answer."""
    if level is None:
        return ""
    return (
        f"\nHELP LADDER STATE: your previous answer in this conversation was at "
        f"rung {level} ({LEVELS[level]}). If the student is still on the same "
        f"problem, continue from there.\n"
    )


class TagFilter:
    """Hold back the first few streamed characters until the tag is resolved.

    The tag is at most ~16 characters, so the first token or two are delayed
    by one chunk — invisible next to the time to first token.
    """

    def __init__(self, on_chunk: Callable[[str], None]):
        self._on_chunk = on_chunk
        self._buffer = ""
        self._resolved = False

    def __call__(self, text: str) -> None:
        if self._resolved:
            self._on_chunk(text)
            return
        self._buffer += text
        head = self._buffer.lstrip()
        complete = bool(_TAG.match(self._buffer))
        # Anything that has stopped looking like the start of a tag is text.
        not_a_tag = bool(head) and not (head.startswith(_PREFIX) or _PREFIX.startswith(head))
        if complete or not_a_tag or len(self._buffer) > _MAX_TAG:
            self._resolved = True
            _, _, rest = parse(self._buffer)
            if rest:
                self._on_chunk(rest)

    def flush(self) -> None:
        """Release anything still held — an answer shorter than a tag."""
        if not self._resolved:
            self._resolved = True
            _, _, rest = parse(self._buffer)
            if rest:
                self._on_chunk(rest)
