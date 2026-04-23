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
- Avoid equations unless absolutely necessary.
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


def build_system_and_user(
    user_message: str,
    context: list,
    mode: str,
    memory: list = None,
    course_persona: str = None,
    course_domain_topics: list = None,
) -> tuple[str, str]:
    """Return ``(system_text, user_text)`` for a RAG turn.

    ``system_text`` contains the stable persona/domain/mode instruction and
    should be passed with ``cache_control`` so Anthropic caches it across turns.
    ``user_text`` contains the dynamic context, memory, and student question.
    """
    system_text = _build_system_text(mode, course_persona, course_domain_topics)

    memory_block = ""
    if memory:
        formatted_memory = "\n".join(memory[-6:])
        memory_block = f"\nCONVERSATION MEMORY:\n{formatted_memory}\n"

    context_block = "\n\n".join(context)
    reference_block = f"""
REFERENCE MATERIAL:
{context_block}

Use this material to ensure factual accuracy.
Do not quote verbatim unless necessary.
"""

    user_text = f"{memory_block}\n{reference_block}\nSTUDENT QUESTION:\n{user_message}".strip()

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
