# Multi-Level Education Architecture

## Overview

This document covers the feasibility, design, and trade-offs of expanding the Meraki AI tutoring backend to support all levels of formal education — from primary school through university — with specific consideration of the West African school structure (Ghana, Nigeria, and similar systems).

**Verdict: Yes, fully achievable.** The current architecture requires only two new database columns and admin-level content configuration. No routers, Celery tasks, or core AI logic need to change.

---

## West African School Level Mapping

| Level | Ghana (GES) | Nigeria (UBE/WAEC) | Suggested `education_level` value |
|---|---|---|---|
| Early childhood | KG1 – KG2 | Nursery 1 – 2 | `kindergarten` *(see Cons)* |
| Primary | Primary 1 – 6 | Primary 1 – 6 | `primary` |
| Junior secondary | JHS 1 – 3 (BECE) | JSS 1 – 3 | `jhs` |
| Senior secondary | SHS 1 – 3 (WASSCE) | SSS 1 – 3 (WAEC/NECO) | `shs` |
| Tertiary | University / Polytechnic | University / Polytechnic | `university` |

---

## Recommended Architecture: Education Level as a Course Attribute

All adaptation is driven through the `courses` table. The three AI modes (learn, review, application) and all task/router logic remain untouched.

### Step 1 — Database: Two new columns on `courses`

```sql
ALTER TABLE public.courses
  ADD COLUMN education_level TEXT
    CHECK (education_level IN ('primary', 'jhs', 'shs', 'university'))
    DEFAULT 'university',
  ADD COLUMN education_system TEXT
    CHECK (education_system IN ('west_africa', 'uk', 'us'))
    DEFAULT 'west_africa';
```

Add corresponding RLS and indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_courses_education_level ON public.courses(education_level);
CREATE INDEX IF NOT EXISTS idx_courses_education_system ON public.courses(education_system);
```

---

### Step 2 — Per-Level Difficulty Descriptors

The `difficulty_descriptors` JSONB column already exists on `courses`. Admins populate it when creating a course. The same `Basic / Intermediate / Advanced` labels are reused, but their meaning is calibrated per level.

**Example — JHS 2 Integrated Science (Ghana):**
```json
{
  "Basic":        "Year 7–8 level. Single-concept recall questions. Everyday Ghanaian language only. No jargon.",
  "Intermediate": "Year 9 level. Two-concept connections, real-life local examples (e.g. mining, farming, market).",
  "Advanced":     "JHS 3 / BECE prep. Exam-style questions from past BECE papers. Multi-step reasoning required."
}
```

**Example — SHS 2 Elective Chemistry (Ghana/WASSCE):**
```json
{
  "Basic":        "Core definitions and simple reactions covered in SHS 1.",
  "Intermediate": "SHS 2 syllabus: stoichiometry, equilibrium, organic intro.",
  "Advanced":     "WASSCE past-question style. Multi-step problems, essay-type questions."
}
```

**Example — University Mining Engineering:**
```json
{
  "Basic":        "Foundational concepts: mineral identification, basic flotation chemistry.",
  "Intermediate": "Process design: reagent selection, circuit optimisation, mass balancing.",
  "Advanced":     "Research-level: novel collector synthesis, plant simulation, HSE case studies."
}
```

---

### Step 3 — Course Persona (existing `persona` field)

The `persona` column on `courses` already flows directly into the AI system prompt via `prompt_builder.py`. No code change is needed — the persona encodes everything the model needs to know about the level.

**JHS persona example:**
```
You are a friendly and patient tutor for JHS 2 students in Ghana.
You speak in simple, clear English.
You use everyday Ghanaian examples: farming, market prices, local rivers, familiar foods.
You explain one idea at a time. You never use technical jargon without immediately explaining it.
You encourage students by name when possible and celebrate correct answers warmly.
```

**SHS persona example:**
```
You are a knowledgeable SHS Chemistry tutor preparing students for WASSCE.
You use precise scientific vocabulary appropriate for SHS 2–3.
You link concepts to real industries in West Africa (mining, oil & gas, food processing).
You reference past WASSCE question styles when explaining exam technique.
```

**University persona example:**
```
You are a university-level lecturer in Mining Engineering.
You use precise technical vocabulary and expect students to have Senior Secondary chemistry.
You cite established principles and, where relevant, reference academic literature.
You push students to reason from first principles rather than memorise.
```

---

### Step 4 — Review Mode Question Type by Level (Frontend Responsibility)

The `session_type` parameter sent by the frontend when starting a review mode session determines the question format. Recommended defaults by level:

| Level | Recommended `session_type` | Rationale |
|---|---|---|
| Primary | `true_false` | Simple binary choice, builds confidence |
| JHS | `true_false`, `fill_blank` | Structured, low open-endedness |
| SHS | `mcq`, `short_answer` | Aligns with WASSCE/NECO exam format |
| University | `short_answer`, `mcq` | Tests depth, analytical writing |

The backend handles all four types identically — only the frontend needs to set the default `session_type` based on the course's `education_level`.

---

### Step 5 — Optional: Response Length Tuning by Level

The learn mode currently uses `max_tokens: 1000` for all courses. If needed, `max_tokens` can be driven by `education_level` without any API surface change — handled inside the Celery task before dispatching to the AI.

| Level | Suggested `max_tokens` | Rationale |
|---|---|---|
| Primary | 400 | Short paragraphs, simple vocabulary |
| JHS | 600 | One concept at a time, a few paragraphs |
| SHS | 800 | Structured explanation with examples |
| University | 1000 (current) | Full technical depth |

---

## What Does NOT Need to Change

| Component | Status |
|---|---|
| All three API routers (`/rag`, `/mode-sessions`, `/sessions`) | Unchanged |
| Celery task definitions (`tasks.py`) | Unchanged |
| RAG pipeline (retriever, Pinecone, embedder) | Unchanged |
| Prompt caching infrastructure | Unchanged — works across all levels |
| Review generation and evaluation logic | Unchanged |
| Application scenario generation | Unchanged |
| Auth, RLS, analytics | Unchanged |

---

## Pros

### 1. Zero core-code changes
All adaptation lives in course configuration — the persona, difficulty descriptors, and uploaded documents. A course admin adding a JHS 2 science course configures it the same way as a university mining course; the AI adjusts automatically.

### 2. Curriculum alignment is natural
The content the AI teaches comes from the documents uploaded to the course (RAG). If you upload the GES Primary 6 Science curriculum PDF, the AI teaches to that exact syllabus. BECE past papers → BECE preparation. WASSCE syllabus → WASSCE preparation. No additional engineering is required.

### 3. Shared infrastructure, lower operational cost
One backend, one Redis cluster, one Celery worker pool, one Supabase instance serves all levels. Economies of scale improve as you add more courses and levels.

### 4. Review mode already supports level-appropriate formats
The four review types (MCQ, fill-in-the-blank, true/false, short-answer) span the full range from primary school to university. No new question types needed for West African curricula.

### 5. Difficulty calibration is already per-course
Because `difficulty_descriptors` is per-course JSON, a "Basic" JHS question and a "Basic" university question are independently defined. No global difficulty taxonomy conflict.

### 6. Multi-country West Africa support
West African school systems follow similar structures (JSS/SSS, JHS/SHS) with local exam bodies (WAEC, BECE, NECO, WASSCE). Since the course persona and uploaded documents carry all the local specifics, the same backend serves Ghana, Nigeria, Sierra Leone, Gambia, etc. without any per-country engineering.

---

## Cons and Hard Limits

### 1. Kindergarten is a different product
Children aged 4–6 cannot type. They require:
- Voice input (microphone → STT → AI → TTS)
- Image/illustration-based content
- Responses of 5–10 words maximum
- A completely different frontend UI

The current text-heavy RAG pipeline and long-form learn-mode responses are fundamentally incompatible with this age group. Scope kindergarten as a separate product decision — it shares almost no backend code with the current system.

**Recommendation:** Exclude `kindergarten` from the `education_level` enum for now. Add it only when a dedicated voice-first interface exists.

### 2. Content safety obligations for minors
When users are under 13 (Primary 1–6) or under 18 (JHS, SHS), you take on legal obligations:
- COPPA (US) or equivalent: parental consent for data collection
- Content filtering: no adult themes, violence, or off-topic conversations
- Data retention limits for minors' data
- Ghana's Data Protection Act 2012 and Nigeria's NDPR may impose additional requirements

None of these exist in the current system. This is the most significant compliance gap and should be resolved before launching to primary or JHS students.

### 3. "Basic / Intermediate / Advanced" is level-relative
If you build cross-course analytics (e.g., "average score at Basic difficulty"), the numbers are meaningless without filtering by `education_level`. A Basic JHS question and a Basic university question are categorically different. Dashboards and KPI queries must always include `education_level` as a filter.

### 4. Document quality varies by level
University lecturers upload well-structured PDFs. Primary school teachers may upload:
- Scanned handouts with low image quality
- Hand-drawn diagrams
- Mixed-language documents (English + local language)

The current PDF ingestion pipeline (text extractor → chunker → embedder) will produce poor chunks from low-quality scans. OCR improvements (e.g., Tesseract, Google Document AI) should be considered before launching to primary/JHS courses.

### 5. Cost scales with student volume, not level complexity
A primary school class of 40 pupils each doing 10 review questions = 400 Haiku API calls. The per-question cost is identical to a university student. Pricing/packaging for schools (which typically have tighter budgets than universities) needs to account for high query volume at lower per-question value.

### 6. Application mode needs level-appropriate scenarios
The current application scenario prompt generates professional/industry-level scenarios by default. For JHS 2 science, "a lab experiment at a mining company" is not relatable. The course persona must explicitly instruct the model to use age-appropriate, locally relevant scenarios (school lab, market, farm). This is a configuration concern, not a code concern — but admins must be made aware of it.

---

## Implementation Sequence

| Phase | Scope | Effort |
|---|---|---|
| **1 (Now)** | Add `education_level` + `education_system` columns to `courses`. Update admin course-creation UI to include these fields. Create first SHS and university courses as proof of concept. | 1–2 days |
| **2 (Short-term)** | JHS support: create JHS personas and difficulty descriptors, upload GES/NERDC curriculum PDFs, validate review question quality at JHS level with a pilot group. | 1–2 weeks |
| **3 (Medium-term)** | Primary support: content safety filtering, minor-user consent flows, document OCR improvements, shortened response `max_tokens`. | 3–6 weeks |
| **4 (Long-term)** | Kindergarten: voice-first interface, TTS integration, image-based content — effectively a new frontend product. | Separate scoping exercise |

---

## Summary

The backend is ready for SHS and university today with only the two SQL columns and course configuration. JHS follows immediately after with the same pattern. Primary requires compliance work before launch. Kindergarten is a separate product.

The West African school structure (GES, NERDC, WAEC, BECE, WASSCE) maps cleanly onto the existing course + persona + difficulty-descriptor model. No architectural rework is needed — only content and configuration.
