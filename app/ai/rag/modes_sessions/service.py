# app/ai/rag/modes_sessions/service.py
import json
import re
from typing import Any, Dict, List, Tuple, Optional, Callable, Awaitable

from app.ai.rag.claude import generate_response
from app.ai.rag.retriever import retrieve_context


# -------------------------
# Static cached system prompts
# -------------------------
# These are byte-identical across ALL courses / difficulty levels, so a single
# Anthropic cache entry covers every call of the same type (near-100% hit rate
# after the first request).  Dynamic content (course, difficulty, ref material)
# lives entirely in the user message.

_SYS_MCQ = """You are an expert educational content creator generating MCQ review items.

Return ONLY raw JSON (no markdown, no fences, no extra text) in this exact schema:
{
  "type": "mcq",
  "question_id": "REV-MCQ-XXXX",
  "difficulty": "<difficulty level>",
  "category": "<topic derived from reference>",
  "question": "<question text>",
  "options": {"A": "...", "B": "...", "C": "...", "D": "..."}
}

Rules:
- Base ALL content ONLY on the REFERENCE MATERIAL in the user message.
- Do NOT include the correct answer explicitly.
- Options must be clearly distinct.
- Keep wording exam-like and concise.
- Set "difficulty" to the level stated in the user message.
- Derive "category" from the subject matter in the reference.""".strip()

_SYS_FILL_BLANK = """You are an expert educational content creator generating fill-in-the-blank review items.

Return ONLY raw JSON (no markdown, no fences, no extra text) in this exact schema:
{
  "type": "fill_blank",
  "item_id": "REV-FB-XXXX",
  "difficulty": "<difficulty level>",
  "category": "<topic derived from reference>",
  "sentence_with_blank": "<sentence using ____ as the blank>"
}

Rules:
- Base ALL content ONLY on the REFERENCE MATERIAL in the user message.
- Do NOT include the answer.
- Use exactly ONE blank (____).
- Keep the sentence short and clear.
- Set "difficulty" to the level stated in the user message.
- Derive "category" from the subject matter in the reference.""".strip()

_SYS_TRUE_FALSE = """You are an expert educational content creator generating True/False review questions.

Generate a single factual statement about a course topic. The statement may be TRUE or FALSE — vary this across questions for pedagogical balance.

Return ONLY raw JSON (no markdown, no fences, no extra text) in this exact schema:
{
  "type": "true_false",
  "question_id": "REV-TF-XXXX",
  "difficulty": "<difficulty level>",
  "category": "<topic derived from reference>",
  "statement": "<a clear, unambiguous factual statement>",
  "is_correct": true,
  "explanation": "<1–2 sentence explanation of why the statement is true or false>"
}

Rules:
- Base ALL content ONLY on the REFERENCE MATERIAL in the user message.
- The statement must be clearly TRUE or FALSE — avoid ambiguous wording.
- If false, introduce exactly ONE specific, plausible misconception derived from the reference.
- Set "is_correct" to true if the statement is factually correct per the reference, false otherwise.
- "explanation" must be 1–2 sentences and cite the reference implicitly.
- Set "difficulty" to the level stated in the user message.
- Derive "category" from the subject matter in the reference.""".strip()

_SYS_SHORT_ANSWER = """You are an expert educational content creator generating short-answer review questions.

Return ONLY raw JSON (no markdown, no fences, no extra text) in this exact schema:
{
  "type": "short_answer",
  "question_id": "REV-SA-XXXX",
  "difficulty": "<difficulty level>",
  "category": "<topic derived from reference>",
  "question": "<question text>",
  "expected_points": ["point1", "point2", "point3"]
}

Rules:
- Base ALL content ONLY on the REFERENCE MATERIAL in the user message.
- expected_points should be 3–6 SHORT bullets (max 12 words each).
- Do NOT include the answer.
- Keep wording exam-like and concise.
- Set "difficulty" to the level stated in the user message.
- Derive "category" from the subject matter in the reference.""".strip()

_SYS_REVIEW_EVAL = """You are an expert educational evaluator grading student review answers.

Rubric bands:
- Excellent (0.90–1.00)
- Good (0.75–0.89)
- Satisfactory (0.60–0.74)
- Needs Improvement (0.40–0.59)
- Unsatisfactory (0.00–0.39)

Return ONLY raw JSON (no markdown, no fences) in this exact schema:
{
  "verdict": "correct|partial|incorrect",
  "score": 0.0,
  "rubric_level": "Excellent|Good|Satisfactory|Needs Improvement|Unsatisfactory",
  "feedback": "<clear concise feedback>",
  "missing_points": ["..."],
  "unsupported_claims": ["..."],
  "correct_answer": "<optional — include if MCQ or fill_blank>"
}

Rules:
- Grade ONLY using the REFERENCE MATERIAL in the user message.
- List claims not in the reference under unsupported_claims.
- For MCQ: student_answer may be A/B/C/D; infer correctness from the reference.""".strip()

_SYS_APP_GEN = """You are an expert educational content creator generating structured APPLICATION scenarios.

Return ONLY raw JSON (no markdown, no fences, no extra text) in this exact schema:
{
  "type": "<session_type>",
  "scenario_id": "PRAC-XXXX",
  "title": "<short title, max 10 words>",
  "difficulty": "<difficulty level>",
  "situation": "<immersive narrative, max 140 words>",
  "available_data": ["...", "...", "...", "..."],
  "guided_questions": ["Q1", "Q2", "Q3"],
  "expected_elements": [
    ["short bullet", "short bullet", "short bullet"],
    ["short bullet", "short bullet", "short bullet"],
    ["short bullet", "short bullet", "short bullet"]
  ],
  "key_learning_points": ["short point", "short point"]
}

Rules:
- Base all technical facts ONLY on the REFERENCE MATERIAL in the user message.
- guided_questions MUST be EXACTLY 3.
- expected_elements MUST be length 3, each inner list 3–5 SHORT bullets.
- Each expected bullet MUST be <= 16 words. NO long sentences. NO paragraphs.
- available_data MUST be 4–6 items only, each <= 12 words.
- key_learning_points MUST be EXACTLY 2 items, each <= 12 words.
- title MUST be <= 10 words.
- Avoid inventing numeric values unless present in REFERENCE MATERIAL.
- Entire JSON response MUST be <= 350 words.""".strip()

_SYS_APP_EVAL = """You are an expert educational evaluator assessing student APPLICATION answers.

Return ONLY raw JSON (no markdown, no fences) in this exact schema:
{
  "verdict": "correct|partial|incorrect",
  "score": 0.0,
  "feedback": "<short clear feedback>",
  "missing_points": ["..."],
  "unsupported_claims": ["..."]
}

Rules:
- Grade ONLY using the REFERENCE MATERIAL in the user message.
- Be supportive and corrective.
- List claims beyond the reference under unsupported_claims.""".strip()


def _make_system(text: str) -> list:
    """Wrap a system prompt string in the cache_control block expected by the SDK."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# -------------------------
# JSON Robust Utilities
# -------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _strip_fences(text: str) -> str:
    raw = (text or "").strip()
    m = _JSON_FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    return raw


def _extract_first_object(raw: str) -> Optional[str]:
    """
    Best-effort extraction of the first top-level JSON object using brace
    balancing.  Returns None if it appears truncated.
    """
    raw = (raw or "").strip()
    start = raw.find("{")
    if start == -1:
        return None

    depth = 0
    in_str = False
    esc = False

    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]

    return None  # likely truncated


async def safe_parse_json_with_retry(
    text: str,
    regenerate_json_fn: Optional[Callable[[], Awaitable[str]]] = None,
    label: str = "json",
) -> Dict[str, Any]:
    """
    Robust JSON parsing:
      1. Try direct parse
      2. Try extracting first {...} block
      3. Retry once via regenerate_json_fn if provided
    """
    raw = _strip_fences(text)

    try:
        return json.loads(raw)
    except Exception:
        pass

    obj = _extract_first_object(raw)
    if obj:
        try:
            return json.loads(obj)
        except Exception:
            pass

    if regenerate_json_fn is not None:
        retry_text = await regenerate_json_fn()
        raw2 = _strip_fences(retry_text)
        try:
            return json.loads(raw2)
        except Exception:
            obj2 = _extract_first_object(raw2)
            if obj2:
                try:
                    return json.loads(obj2)
                except Exception as e:
                    raise ValueError(
                        f"Invalid {label} JSON after retry: {e}\nRaw:\n{retry_text}"
                    )
        raise ValueError(f"Invalid {label} JSON after retry.\nRaw:\n{retry_text}")

    raise ValueError(f"Invalid {label} JSON.\nRaw:\n{text}")


# -------------------------
# Difficulty
# -------------------------

def adjust_difficulty(current: str, score: float) -> str:
    levels = ["Basic", "Intermediate", "Advanced"]
    current = (current or "").strip().title()
    if current not in levels:
        current = "Basic"
    idx = levels.index(current)

    if score >= 0.85 and idx < 2:
        idx += 1
    elif score < 0.45 and idx > 0:
        idx -= 1

    return levels[idx]


# -------------------------
# Difficulty descriptor helper
# -------------------------

def _difficulty_note(difficulty_descriptors: Dict[str, Any], difficulty: str) -> str:
    """
    Returns a one-line note describing what the given difficulty level means
    for this course.  Falls back gracefully if descriptors are missing.
    """
    if not difficulty_descriptors:
        return ""
    desc = difficulty_descriptors.get(difficulty, "")
    if not desc:
        return ""
    return f"\nDifficulty definition for this course — {difficulty}: {desc}"


# -------------------------
# Application scenario validators
# -------------------------

def _ensure_three_guided_questions(scenario: Dict[str, Any]) -> None:
    gq = scenario.get("guided_questions") or []
    if not isinstance(gq, list):
        raise ValueError("Scenario invalid: guided_questions must be a list")
    if len(gq) < 3:
        raise ValueError(
            "Scenario invalid: guided_questions must contain EXACTLY 3 questions"
        )
    scenario["guided_questions"] = gq[:3]


def _ensure_expected_elements_shape(scenario: Dict[str, Any]) -> None:
    ee = scenario.get("expected_elements") or []
    if not isinstance(ee, list):
        raise ValueError("Scenario invalid: expected_elements must be a list")
    if len(ee) < 3:
        raise ValueError(
            "Scenario invalid: expected_elements must be length 3 "
            "(one list per guided question)"
        )
    scenario["expected_elements"] = ee[:3]

    for i in range(3):
        inner = scenario["expected_elements"][i]
        if not isinstance(inner, list):
            scenario["expected_elements"][i] = []
            inner = scenario["expected_elements"][i]
        inner = inner[:5]
        cleaned = []
        for b in inner:
            s = re.sub(r"\s+", " ", str(b)).strip()
            if len(s) > 120:
                s = s[:120].rsplit(" ", 1)[0] + "..."
            cleaned.append(s)
        scenario["expected_elements"][i] = cleaned


# =========================================================
# REVIEW GENERATORS
# =========================================================

async def generate_review_item(
    session_type: str,
    difficulty: str,
    course_id: str,
    course_name: str,
    difficulty_descriptors: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Generate one structured review item grounded in retrieved reference material."""
    session_type = (session_type or "").lower().strip()
    if session_type == "mcqs":
        session_type = "mcq"
    if session_type == "flashcard":
        session_type = "true_false"
    difficulty = (difficulty or "").strip().title() or "Basic"
    diff_note = _difficulty_note(difficulty_descriptors, difficulty)

    seed = f"{course_name} review assessment question type={session_type}, difficulty={difficulty}"
    contexts = await retrieve_context(query=seed, mode="review", course_id=course_id, top_k=6)

    ref = "\n".join(contexts)
    user_ctx = (
        f"Course: {course_name}\n"
        f"Difficulty: {difficulty}{diff_note}\n\n"
        f"REFERENCE MATERIAL:\n{ref}"
    )
    _retry_suffix = "\n\nIMPORTANT: Output ONLY complete raw JSON. No markdown. Must be COMPLETE."

    if session_type == "mcq":
        sys_parts = _make_system(_SYS_MCQ)
        raw = await generate_response(prompt=user_ctx, mode="review_generation", system_parts=sys_parts)
        item = await safe_parse_json_with_retry(
            raw,
            regenerate_json_fn=lambda: generate_response(
                prompt=user_ctx + _retry_suffix,
                mode="review_generation",
                system_parts=sys_parts,
            ),
            label="review_mcq",
        )
        return item, contexts

    if session_type == "fill_blank":
        sys_parts = _make_system(_SYS_FILL_BLANK)
        raw = await generate_response(prompt=user_ctx, mode="review_generation", system_parts=sys_parts)
        item = await safe_parse_json_with_retry(
            raw,
            regenerate_json_fn=lambda: generate_response(
                prompt=user_ctx + _retry_suffix,
                mode="review_generation",
                system_parts=sys_parts,
            ),
            label="review_fill_blank",
        )
        return item, contexts

    if session_type == "true_false":
        sys_parts = _make_system(_SYS_TRUE_FALSE)
        raw = await generate_response(prompt=user_ctx, mode="review_generation", system_parts=sys_parts)
        item = await safe_parse_json_with_retry(
            raw,
            regenerate_json_fn=lambda: generate_response(
                prompt=user_ctx + _retry_suffix,
                mode="review_generation",
                system_parts=sys_parts,
            ),
            label="review_true_false",
        )
        # Normalise is_correct to a Python bool regardless of what the LLM returned
        item["is_correct"] = bool(item.get("is_correct", True))
        return item, contexts

    # default: short_answer
    sys_parts = _make_system(_SYS_SHORT_ANSWER)
    raw = await generate_response(prompt=user_ctx, mode="review_generation", system_parts=sys_parts)
    item = await safe_parse_json_with_retry(
        raw,
        regenerate_json_fn=lambda: generate_response(
            prompt=user_ctx + _retry_suffix,
            mode="review_generation",
            system_parts=sys_parts,
        ),
        label="review_short_answer",
    )
    return item, contexts


async def evaluate_review_answer(
    item: Dict[str, Any],
    student_answer: str,
    contexts: List[str],
    course_name: str,
) -> Dict[str, Any]:
    reference = "\n\n".join(contexts)
    itype = item.get("type")

    if itype == "mcq":
        qtext = item["question"]
        options = item["options"]
        qblock = qtext + "\nOptions:\n" + "\n".join(
            f"{k}) {v}" for k, v in options.items()
        )
    elif itype == "fill_blank":
        qblock = item["sentence_with_blank"]
    elif itype == "true_false":
        qblock = item["statement"]
    else:
        qblock = item.get("question", "")

    # ── True/False: binary correctness is deterministic; LLM grades explanation ──
    if itype == "true_false":
        is_correct: bool = bool(item.get("is_correct", True))
        correct_label = "True" if is_correct else "False"
        explanation = item.get("explanation", "")

        # Normalise the student's binary choice
        answer_lower = student_answer.lower().strip()
        if answer_lower.startswith("true") or answer_lower == "t":
            student_picked = "True"
        elif answer_lower.startswith("false") or answer_lower == "f":
            student_picked = "False"
        else:
            student_picked = None  # unclear — let LLM decide

        binary_correct = (student_picked == correct_label) if student_picked else None

        user_msg = (
            f"Course: {course_name}\n"
            f"Item Type: true_false\n\n"
            f"Statement:\n{qblock}\n\n"
            f"Correct answer: {correct_label}\n"
            f"Explanation: {explanation}\n\n"
            f"Student Answer:\n{student_answer}\n\n"
            f"REFERENCE MATERIAL:\n{reference}"
        )
        if binary_correct is not None:
            user_msg += (
                f"\n\nNote: The student's binary choice ({student_picked}) is "
                f"{'CORRECT' if binary_correct else 'INCORRECT'}. "
                f"Grade their explanation quality accordingly."
            )

        raw = await generate_response(
            prompt=user_msg, mode="review", system_parts=_make_system(_SYS_REVIEW_EVAL)
        )
        result = await safe_parse_json_with_retry(raw, label="review_evaluation_tf")

        # Override verdict/score if binary choice was clearly wrong
        if binary_correct is False:
            result["verdict"] = "incorrect"
            result["score"] = min(float(result.get("score", 0.0)), 0.35)
        elif binary_correct is True and result.get("verdict") == "incorrect":
            result["verdict"] = "partial"
            result["score"] = max(float(result.get("score", 0.5)), 0.5)

        result["correct_answer"] = correct_label
        return result

    expected_points = item.get("expected_points", [])

    user_msg = (
        f"Course: {course_name}\n"
        f"Item Type: {itype}\n\n"
        f"Prompt/Question:\n{qblock}\n\n"
        f"Student Answer:\n{student_answer}\n\n"
        f"Expected points (if provided):\n{json.dumps(expected_points)}\n\n"
        f"REFERENCE MATERIAL:\n{reference}"
    )
    raw = await generate_response(
        prompt=user_msg, mode="review", system_parts=_make_system(_SYS_REVIEW_EVAL)
    )
    return await safe_parse_json_with_retry(raw, label="review_evaluation")


def format_review_prompt(item: Dict[str, Any]) -> str:
    itype = item.get("type")

    if itype == "mcq":
        opts = item["options"]
        opt_lines = "\n".join(f"{k}) {v}" for k, v in opts.items())
        return (
            f"### REVIEW (MCQ)\n"
            f"Question ID: {item.get('question_id', '')}\n"
            f"Difficulty: {item.get('difficulty', '')}\n"
            f"Category: {item.get('category', '')}\n\n"
            f"**Question:**\n{item['question']}\n\n"
            f"**Options:**\n{opt_lines}\n"
            f"\nReply with A, B, C, or D."
        )

    if itype == "fill_blank":
        return (
            f"### REVIEW (Fill-in)\n"
            f"Item ID: {item.get('item_id', '')}\n"
            f"Difficulty: {item.get('difficulty', '')}\n"
            f"Category: {item.get('category', '')}\n\n"
            f"**Fill the blank:**\n{item['sentence_with_blank']}\n"
        )

    if itype == "true_false":
        return (
            f"### REVIEW (True/False)\n"
            f"Question ID: {item.get('question_id', '')}\n"
            f"Difficulty: {item.get('difficulty', '')}\n"
            f"Category: {item.get('category', '')}\n\n"
            f"**Statement:**\n{item['statement']}\n\n"
            f"Reply with **True** or **False**, then briefly explain your reasoning in 1–2 sentences."
        )

    # short_answer (default)
    return (
        f"### REVIEW (Short Answer)\n"
        f"Question ID: {item.get('question_id', '')}\n"
        f"Difficulty: {item.get('difficulty', '')}\n"
        f"Category: {item.get('category', '')}\n\n"
        f"**Question:**\n{item.get('question', '')}\n"
    )


# =========================================================
# APPLICATION GENERATORS
# =========================================================

async def generate_application_scenario(
    session_type: str,
    difficulty: str,
    course_id: str,
    course_name: str,
    difficulty_descriptors: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Generate a structured multi-step application scenario grounded in retrieved reference."""
    session_type = (session_type or "").lower().strip()
    difficulty = (difficulty or "").strip().title() or "Basic"
    diff_note = _difficulty_note(difficulty_descriptors, difficulty)

    seed = f"{course_name} practice scenario type={session_type}, difficulty={difficulty}"
    contexts = await retrieve_context(
        query=seed, mode="application", course_id=course_id, top_k=6
    )

    ref = "\n".join(contexts)
    sys_parts = _make_system(_SYS_APP_GEN)
    user_msg = (
        f"Course: {course_name}\n"
        f"Session type: {session_type}\n"
        f"Difficulty: {difficulty}{diff_note}\n\n"
        f"REFERENCE MATERIAL:\n{ref}"
    )
    _retry_suffix = (
        "\n\nIMPORTANT: Output ONLY COMPLETE raw JSON (no markdown). "
        "Keep it SHORT and compact. Ensure all strings are closed and JSON is complete."
    )

    raw = await generate_response(prompt=user_msg, mode="application", system_parts=sys_parts)
    scenario = await safe_parse_json_with_retry(
        raw,
        regenerate_json_fn=lambda: generate_response(
            prompt=user_msg + _retry_suffix,
            mode="application",
            system_parts=sys_parts,
        ),
        label="application_scenario",
    )

    scenario["type"] = session_type
    scenario["difficulty"] = difficulty
    _ensure_three_guided_questions(scenario)
    _ensure_expected_elements_shape(scenario)

    return scenario, contexts


async def evaluate_application_answer(
    scenario: Dict[str, Any],
    step: int,
    student_answer: str,
    contexts: List[str],
    course_name: str,
) -> Dict[str, Any]:
    reference = "\n\n".join(contexts)
    idx = step - 1

    question = scenario["guided_questions"][idx]
    expected = scenario["expected_elements"][idx]
    expected_block = "\n".join(f"- {x}" for x in expected)

    user_msg = (
        f"Course: {course_name}\n\n"
        f"Scenario:\n{scenario.get('situation', '')}\n\n"
        f"Guided Question {step}:\n{question}\n\n"
        f"Student Answer:\n{student_answer}\n\n"
        f"Expected elements:\n{expected_block}\n\n"
        f"REFERENCE MATERIAL:\n{reference}"
    )
    raw = await generate_response(
        prompt=user_msg, mode="review", system_parts=_make_system(_SYS_APP_EVAL)
    )
    return await safe_parse_json_with_retry(raw, label="application_evaluation")


def format_application_prompt(scenario: Dict[str, Any], step: int) -> str:
    idx = step - 1
    data_lines = "\n".join(f"- {x}" for x in scenario.get("available_data", []))
    q = scenario["guided_questions"][idx]

    return (
        f"### PRACTICE ({scenario.get('type', '')})\n"
        f"Scenario ID: {scenario.get('scenario_id', '')}\n"
        f"Title: {scenario.get('title', '')}\n"
        f"Difficulty: {scenario.get('difficulty', '')}\n\n"
        f"**SITUATION:**\n{scenario.get('situation', '')}\n\n"
        f"**Available Data:**\n{data_lines}\n\n"
        f"**Guided Question {step}:**\n{q}\n"
    )
