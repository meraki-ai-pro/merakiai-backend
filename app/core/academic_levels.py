"""Academic levels, in the vocabulary Ghanaian higher education actually uses.

A student at the University of Ghana or KNUST says "I'm in Level 200", not
"I'm at the intermediate tier". The lecturer writes course codes like MATH 103
and TECH 201. Using a generic ladder would mean everyone translating between
our words and theirs at every screen, which is exactly the kind of friction
that gets a pilot abandoned.

So the LEVEL is Ghanaian and user-facing. The SCAFFOLDING TIER is an internal
grouping — several levels teach the same way, and the prompt only needs to know
how much of the reasoning to do for the student.

That separation is what keeps this extensible: adding a level for another
country's system is a row here, not a rewrite of the teaching prompts.

Ref: AI_Teaching_System_Project_Proposal §2 (the academic ladder)
"""

from __future__ import annotations

from typing import Final, Literal

AcademicLevel = Literal[
    "access",
    "level_100",
    "level_200",
    "level_300",
    "level_400",
    "level_500",
    "level_600",
    "hnd",
    "masters",
    "doctoral",
]

# Scaffolding tiers. Deliberately fewer than levels: Level 100 and Level 200
# are taught the same way, and duplicating the instruction for each would let
# them drift apart for no reason.
Tier = Literal["foundation", "intermediate", "advanced", "masters", "doctoral"]


class Level:
    __slots__ = ("code", "label", "short", "tier", "note")

    def __init__(self, code: str, label: str, short: str, tier: str, note: str = ""):
        self.code = code
        self.label = label
        self.short = short
        self.tier = tier
        self.note = note


# Ordered as a student progresses. The order is used for the UI dropdown, so
# it should read like a prospectus, not like an alphabetised list.
LEVELS: Final[tuple[Level, ...]] = (
    Level(
        "access", "Access / Foundation year", "Access", "foundation",
        "Pre-degree and mature-entry students preparing for Level 100.",
    ),
    Level(
        "level_100", "Level 100 (First year)", "L100", "foundation",
        "Pilot cohort.",
    ),
    Level(
        "level_200", "Level 200 (Second year)", "L200", "foundation",
        "Pilot cohort.",
    ),
    Level("level_300", "Level 300 (Third year)", "L300", "intermediate"),
    Level("level_400", "Level 400 (Final year)", "L400", "advanced"),
    Level(
        "level_500", "Level 500 (Fifth year)", "L500", "advanced",
        "Longer professional programmes — Medicine, Pharmacy, Architecture, "
        "Veterinary Medicine.",
    ),
    Level(
        "level_600", "Level 600 (Sixth year)", "L600", "advanced",
        "Final clinical years of Medicine and related programmes.",
    ),
    Level(
        "hnd", "HND / Diploma", "HND", "foundation",
        "Technical universities. Applied and practical rather than theoretical, "
        "so worked examples matter more than derivations.",
    ),
    Level("masters", "Masters (MPhil / MSc / MA / MBA)", "Masters", "masters"),
    Level("doctoral", "Doctoral (PhD)", "PhD", "doctoral"),
)

BY_CODE: Final[dict[str, Level]] = {lvl.code: lvl for lvl in LEVELS}

LEVEL_CODES: Final[tuple[str, ...]] = tuple(lvl.code for lvl in LEVELS)


def normalise(value: str | None) -> str | None:
    """Accept the ways a level gets typed and return the canonical code.

    Lecturers will write "Level 200", "L200", "200" or "level-200" depending on
    the day. Rejecting all but one spelling would be pedantry, not validation.
    """
    if not value:
        return None

    raw = str(value).strip().lower().replace("-", "_").replace(" ", "_")

    if raw in BY_CODE:
        return raw

    # "l200" / "200" / "level200"
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits and f"level_{digits}" in BY_CODE:
        return f"level_{digits}"

    aliases = {
        "phd": "doctoral",
        "mphil": "masters", "msc": "masters", "ma": "masters", "mba": "masters",
        "diploma": "hnd", "higher_national_diploma": "hnd",
        "foundation": "access", "foundation_year": "access",
        "first_year": "level_100", "second_year": "level_200",
        "third_year": "level_300", "final_year": "level_400",
    }
    return aliases.get(raw)


def tier_for(value: str | None) -> str | None:
    """Scaffolding tier for a level code. None when unknown."""
    code = normalise(value)
    return BY_CODE[code].tier if code else None


def as_options() -> list[dict[str, str]]:
    """For the lecturer UI dropdown, in progression order."""
    return [
        {"code": lvl.code, "label": lvl.label, "short": lvl.short,
         "tier": lvl.tier, "note": lvl.note}
        for lvl in LEVELS
    ]
