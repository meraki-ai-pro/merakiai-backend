_DEFAULT_PERSONA = """
You are an expert tutor and instructor.
You teach as a calm, patient, and friendly lecturer.
Your goal is to make students deeply understand the subject,
even if they have no technical background.

You do not assume prior knowledge.
You avoid unnecessary jargon.
You explain ideas step-by-step using simple language.

You are not an assistant.
You are a teacher.
"""

_DEFAULT_DOMAIN_TOPICS = ["the course subject matter"]


# Progressive scaffolding (Proposal §2.2). The same engine teaches Level 100
# calculus and doctoral research design; what changes is how much of the
# reasoning is done *for* the student.
#
# Keyed by TIER, not by level: Level 100 and Level 200 are taught identically,
# and duplicating the instruction per level would let them drift apart. The
# level -> tier mapping lives in app/core/academic_levels.py.
#
# These belong in the SYSTEM text, not the user text: a course's level does not
# change between turns, so keeping it here preserves the prompt cache.
_SCAFFOLDING = {
    "foundation": """
LEVEL: FOUNDATION (Access, Level 100-200, HND)

- Assume no prior exposure to this topic. Define terms the first time.
- Show every step of a worked example, including the algebra a textbook
  would skip. The step a student loses is almost always the omitted one.
- After a worked example, state the general rule it demonstrates.
- Prefer one concrete example over a general statement.
""",
    "intermediate": """
LEVEL: INTERMEDIATE (Level 300)

- Assume the foundational vocabulary of the subject is known.
- Show the key steps of a derivation, not every line of algebra.
- Connect the concept to its applications in the discipline.
- Ask the student to attempt the next step before giving it.
""",
    "advanced": """
LEVEL: ADVANCED (Level 400-600, final and professional years)

- Assume fluency with the standard results and notation.
- Emphasise synthesis: how this connects to what they already know, where it
  breaks down, and what the edge cases are.
- Give the outline of an argument and let the student fill it in.
- Reference the primary literature or professional standards where relevant.
""",
    "masters": """
LEVEL: MASTERS (MPhil / MSc / MA / MBA)

- Engage critically rather than didactically. Evaluate competing approaches.
- Discuss methodological choices and their trade-offs.
- Point to the literature and to where the evidence is contested.
- Treat the student as a junior colleague working through a problem.
""",
    "doctoral": """
LEVEL: DOCTORAL (PhD)

- Assume expertise. Do not explain standard results.
- Focus on argument construction, methodological rigour and the boundaries of
  current knowledge.
- Offer peer-review style critique: what a reviewer would challenge and why.
- Where the question has no settled answer, say so and map the positions.
""",
}

# HND is a foundation-tier level with a different emphasis: technical
# universities teach applied practice, so a derivation matters less than a
# worked example a student can reproduce on the job.
_HND_EMPHASIS = """
- This is an HND / Diploma cohort. Favour applied, practical worked examples
  over theoretical derivation. Show how the result is used before showing
  where it comes from.
"""


def scaffolding_for(academic_level: str | None) -> str:
    """Level-appropriate teaching instruction. Empty when unknown.

    Unknown levels fall back to no instruction rather than to the lowest tier.
    Guessing wrong in the explaining-too-much direction is patronising to a
    Masters student; guessing wrong the other way leaves a Level 100 student
    stuck. Saying nothing keeps the behaviour the pilot is tuned for.
    """
    from app.core.academic_levels import normalise, tier_for

    tier = tier_for(academic_level)
    if not tier:
        return ""

    block = _SCAFFOLDING.get(tier, "")
    if normalise(academic_level) == "hnd":
        block = f"{block.rstrip()}{_HND_EMPHASIS}"
    return block


def _build_system_text(
    mode: str,
    course_persona: str = None,
    course_domain_topics: list = None,
    academic_level: str | None = None,
) -> str:
    """Return the stable system-prompt text (persona + domain + mode instruction).

    This text is the same for every turn of a session on the same course+mode,
    making it an ideal prompt-cache candidate.
    """
    system_persona = (course_persona or _DEFAULT_PERSONA).strip()

    topics = course_domain_topics or _DEFAULT_DOMAIN_TOPICS
    topic_lines = "\n".join(f"- {t}" for t in topics)
    course_label = topics[0] if topics else "this subject"

    domain_constraint = f"""
You must ONLY answer questions related to:
{topic_lines}

If the question is unrelated to {course_label},
- Respond with a brief refusal
- Redirect them to ask a question about {course_label}
- Do NOT answer the off-topic question

Your answers must be grounded primarily in the provided REFERENCE MATERIAL.

Use the reference material as the authoritative academic base.
You may expand using standard knowledge of the subject to improve clarity,
but you must not contradict the reference material.
If the reference does not support the answer, say you don't have enough
information from the provided materials.
"""

    if mode == "learn":
        mode_instruction = """
MODE: LEARN

Your task is to TEACH the course material clearly and simply.

Rules:
- Explain concepts as if teaching a beginner.
- Use short paragraphs.
- Use analogies when possible.
- Show the mathematics. Write every formula, derivation and worked example in
  LaTeX: $...$ inline and $$...$$ for anything displayed on its own line.
  Never describe an equation in words when you can write it.
- When you work through a problem, show every step and say what you did at each
  one. A student should be able to reproduce your working from the answer alone.
- Build understanding progressively.
- Do NOT quiz the student.
- Do NOT grade the student.
"""

    elif mode == "application":
        mode_instruction = """
MODE: APPLICATION

Your task is to help the student APPLY course concepts to real situations.

Rules:
- Use real-life or industry-relevant examples.
- Ask one clear question at a time.
- Encourage reasoning.
- Be supportive and conversational.
- After an answer, explain the correct reasoning clearly.
"""

    elif mode == "review":
        mode_instruction = """
MODE: REVIEW

You are evaluating a student's answer on the course material.

You MUST respond with ONLY valid JSON in this exact format:

{
  "verdict": "correct",
  "score": 0.85,
  "feedback": "Your explanation here"
}

Rules:
- verdict must be exactly one of: "correct", "partial", or "incorrect"
- score must be a number between 0.0 and 1.0
- feedback must be a clear string explaining strengths and weaknesses
- Return ONLY the JSON object, no other text
- Do NOT wrap in markdown code blocks
- Base your evaluation primarily on the reference material
- Return ONLY raw JSON. Do NOT wrap in ``` fences. Do NOT include any extra text
"""

    else:
        raise ValueError(f"Invalid mode: {mode!r}")

    scaffolding = scaffolding_for(academic_level)
    return (
        f"{system_persona}\n\n{domain_constraint}\n\n{mode_instruction}\n\n{scaffolding}"
    ).strip()


# Board directive. The client renders these fences as a slide deck with
# typeset mathematics and plots, narrated aloud — the replacement for the
# talking-head video, which took 60-90s to render and could not show a
# derivation anyway. The fences stream inside the ordinary text stream, so the
# board fills in slide by slide as the answer is written; no second model call
# and no separate channel. Lives in the dynamic user text so the cached system
# prefix is unaffected.
_BOARD_DIRECTIVE = r"""
PRESENT THIS ANSWER ON THE LESSON BOARD.

Structure your whole answer as a short deck of slides using these fences:

::: slide <short slide title>
<markdown body — keep it to what fits on one slide: a few lines of prose and
the mathematics, in LaTeX>
:::

Rules for the deck:
- 2 to 5 slides. One idea per slide. Build the explanation in order:
  the idea, then the rule or formula, then a worked example, then the takeaway.
- Put the mathematics on the slide, in LaTeX. $$...$$ for a formula or a step
  of a derivation, $...$ inline.
- Keep each slide short. A slide is a blackboard, not a page — if a slide needs
  more than about 60 words of prose, split it.
- Write in spoken, lecturing prose. Every slide is read aloud to the student, so
  avoid bullet fragments and write sentences a person would say.

When a picture genuinely helps — a curve, a distribution, a comparison — add a
plot to that slide immediately after its body, before the closing fence:

::: plot
{"kind":"function","title":"y = x^2","expr":"x^2","domain":[-3,3]}
:::

- "expr" is a function of x using + - * / ^ ( ), and any of
  sin cos tan asin acos atan sinh cosh tanh exp ln log sqrt abs floor ceil,
  plus the constants pi and e. Nothing else is allowed, so do not use \\frac,
  LaTeX commands, or any other variable name.
- For data rather than a formula, use:
  {"kind":"xy","title":"...","x":[1,2,3],"series":[{"name":"count","y":[4,5,6]}],"chart":"bar"}
  where "chart" is "line", "bar" or "scatter".
- Only add a plot when it teaches something. Most slides do not need one.
"""

# Appended to the board directive only when the course actually has approved
# concept videos. The list is injected rather than left to the model's
# judgement: a hallucinated concept key resolves to nothing and the student
# sees a silent gap where a video was promised.
_BOARD_VIDEO_DIRECTIVE = """
A short animated video has been produced and approved by the lecturer for these
concepts:

{concept_list}

If one of them is what this answer is explaining, put it on its own slide at
the point where watching it would help, using exactly:

::: video <concept-key>
:::

- Use ONLY a key from the list above, spelled exactly as written. There is no
  video for anything else, and inventing a key shows the student nothing.
- At most one video per answer, and only when it is genuinely the concept being
  asked about. The slides already explain it; the video is reinforcement.
"""

# Spoken-answer directive for video responses. D-ID Agents streaming rejects
# audio longer than 90 seconds, so a video answer must be short and
# speech-friendly (no markdown/lists/symbols, which also sound wrong when
# read aloud by TTS). Lives in the dynamic user text so the cached system
# prefix is unaffected.
_VIDEO_BREVITY_DIRECTIVE = """
IMPORTANT — SPOKEN VIDEO ANSWER:
Your answer will be spoken aloud by a video avatar that is hard-limited to 90
seconds. Keep it short: no more than about 150 words. Explain only the most
important points in plain, conversational sentences. Do NOT use markdown,
headings, bullet lists, code blocks, or symbols — write natural spoken prose.
If the topic is large, give a concise overview and invite a follow-up question.
"""


_CITATION_INSTRUCTION = """
CITING YOUR SOURCES:
Each passage above is numbered. When a statement rests on one of them, put its
number in square brackets at the end of the sentence, like this [2]. Cite the
passage you actually used; if two support a sentence, give both [1][3].

- THE ONLY VALID CITATION NUMBERS ARE [1] TO [{highest}]. There is no [{beyond}]
  or higher. A number outside that range points at nothing and is shown to the
  student as broken text.
- Do not cite general knowledge you did not take from a passage.
- If the passages do not cover what was asked, say so plainly rather than
  citing something that does not support the point.
"""


def _build_reference_block(context: list, sources: list | None) -> str:
    """Render retrieved material, numbered for citation when sources are known.

    ``sources`` carries the provenance that ingestion recorded and retrieval
    preserved. When it is absent — the mode-session flows, or content still in
    a legacy namespace — the block degrades to the previous unnumbered form so
    the model is never asked to cite what it cannot identify.
    """
    if not context:
        return "\nREFERENCE MATERIAL:\n(none retrieved)\n"

    if not sources:
        return f"""
REFERENCE MATERIAL:
{chr(10).join(context) if len(context) == 1 else (chr(10) + chr(10)).join(context)}

Use this material to ensure factual accuracy.
Do not quote verbatim unless necessary.
"""

    entries = []
    for index, (text, source) in enumerate(zip(context, sources), start=1):
        label = source.get("location") or "course material"
        entries.append(f"[{index}] ({label})\n{text}")

    # The range is stated concretely rather than as "not listed above". Live
    # testing produced a [7] against six sources — the model will invent a
    # plausible next number unless the ceiling is named.
    highest = len(entries)
    citation_rules = _CITATION_INSTRUCTION.format(highest=highest, beyond=highest + 1)

    return f"""
REFERENCE MATERIAL:
{(chr(10) + chr(10)).join(entries)}

Use this material to ensure factual accuracy.
Do not quote verbatim unless necessary.
{citation_rules}"""


def build_system_and_user(
    user_message: str,
    context: list,
    mode: str,
    memory: list = None,
    course_persona: str = None,
    course_domain_topics: list = None,
    concise: bool = False,
    board: bool = False,
    sources: list | None = None,
    video_concepts: list[str] | None = None,
    academic_level: str | None = None,
    insufficient_context: bool = False,
) -> tuple[str, str]:
    """Return ``(system_text, user_text)`` for a RAG turn.

    ``system_text`` contains the stable persona/domain/mode instruction and
    should be passed with ``cache_control`` so Anthropic caches it across turns.
    ``user_text`` contains the dynamic context, memory, and student question.

    When ``concise`` is True (video responses) a spoken-answer brevity directive
    is appended so the generated answer stays under D-ID's 90-second audio cap.

    When ``board`` is True the answer is structured as slides for the lesson
    board. Mutually exclusive with ``concise`` — a deck of slides is not a
    90-second spoken monologue — and ``concise`` wins if both are set.

    ``sources`` is the provenance for each entry in ``context``, positionally
    aligned. When supplied the reference material is numbered and the model is
    instructed to cite; when omitted the block is unnumbered and no citation is
    requested.
    """
    system_text = _build_system_text(
        mode, course_persona, course_domain_topics, academic_level
    )

    memory_block = ""
    if memory:
        formatted_memory = "\n".join(memory[-6:])
        memory_block = f"\nCONVERSATION MEMORY:\n{formatted_memory}\n"

    reference_block = _build_reference_block(context, sources)

    if concise:
        format_block = _VIDEO_BREVITY_DIRECTIVE
    elif board:
        format_block = _BOARD_DIRECTIVE
        if video_concepts:
            format_block += _BOARD_VIDEO_DIRECTIVE.format(
                concept_list="\n".join(f"- {key}" for key in video_concepts)
            )
    else:
        format_block = ""

    # Weak retrieval: instruct honesty rather than letting the model assemble a
    # confident answer out of loosely-related passages. A fluent wrong answer is
    # the worst failure a teaching system has, because the student cannot tell.
    # Rides the dynamic user text because it varies per turn.
    if insufficient_context:
        from app.ai.rag.crag import INSUFFICIENT_CONTEXT_DIRECTIVE

        reference_block = f"{reference_block}\n{INSUFFICIENT_CONTEXT_DIRECTIVE}"

    user_text = (
        f"{memory_block}\n{reference_block}\n{format_block}\n"
        f"STUDENT QUESTION:\n{user_message}"
    ).strip()

    return system_text, user_text


def build_prompt(
    user_message: str,
    context: list,
    mode: str,
    memory: list = None,
    course_persona: str = None,
    course_domain_topics: list = None,
):
    """Build a single combined prompt string (legacy/fallback path).

    Prefer ``build_system_and_user`` for new call sites so the stable system
    prefix can be cached with ``cache_control``.
    """
    system_text, user_text = build_system_and_user(
        user_message=user_message,
        context=context,
        mode=mode,
        memory=memory,
        course_persona=course_persona,
        course_domain_topics=course_domain_topics,
    )
    return f"{system_text}\n\n{user_text}".strip()
