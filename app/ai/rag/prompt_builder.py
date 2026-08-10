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


def _build_system_text(
    mode: str,
    course_persona: str = None,
    course_domain_topics: list = None,
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

    return f"{system_persona}\n\n{domain_constraint}\n\n{mode_instruction}".strip()


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

- Do not cite general mathematical knowledge you did not take from a passage.
- If the passages do not cover what was asked, say so plainly rather than
  citing something that does not support the point.
- Never invent a number that is not listed above.
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

    return f"""
REFERENCE MATERIAL:
{(chr(10) + chr(10)).join(entries)}

Use this material to ensure factual accuracy.
Do not quote verbatim unless necessary.
{_CITATION_INSTRUCTION}"""


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
    system_text = _build_system_text(mode, course_persona, course_domain_topics)

    memory_block = ""
    if memory:
        formatted_memory = "\n".join(memory[-6:])
        memory_block = f"\nCONVERSATION MEMORY:\n{formatted_memory}\n"

    reference_block = _build_reference_block(context, sources)

    if concise:
        format_block = _VIDEO_BREVITY_DIRECTIVE
    elif board:
        format_block = _BOARD_DIRECTIVE
    else:
        format_block = ""

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
