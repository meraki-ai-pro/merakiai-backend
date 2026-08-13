# Golden sets

One `.jsonl` file per course. Each line is a question a lecturer has validated,
labelled with the material that *should* come back.

```bash
python scripts/eval_rag.py evals/froth-flotation.jsonl --out runs/today.json
python scripts/eval_rag.py evals/froth-flotation.jsonl --baseline runs/today.json
```

The second form is the one that matters: it prints which specific questions
regressed and **exits non-zero** if any did, so it can gate a deploy.

## Case format

```json
{"question": "...", "course_id": "...", "mode": "learn", "expect_sources": ["..."], "expect_text": ["..."], "topic": "...", "note": "..."}
```

| field | meaning |
|---|---|
| `question` | asked verbatim, as a student would type it |
| `course_id` | which course to search |
| `mode` | `learn` \| `review` \| `application` (default `learn`) |
| `expect_sources` | substrings matched against filename, section title or heading path |
| `expect_text` | substrings that must appear in a retrieved passage |
| `expect_no_answer` | `true` when the material genuinely does not cover this |
| `topic`, `note` | free text, for humans |

**Expectations are substrings, not chunk ids.** A golden set has to survive
re-ingestion, and chunk ids change every time the chunker does.

## Writing a good set

Aim for **30–50 cases per course**. Below about 20, one question moving swings
the score enough to be meaningless.

Include, in roughly these proportions:

- **Ordinary questions** a student actually asks — phrased casually, not as the
  textbook phrases them. This is what the query rewriter exists for.
- **Exact-term questions** ("what is Theorem 4.2", "the chi-squared statistic").
  These are what dense-only retrieval loses and BM25 recovers.
- **Cross-section questions** whose answer spans two parts of the notes.
- **A few `expect_no_answer` cases.** Questions the course does not cover, where
  the correct behaviour is to admit it. Without these, a system that
  confidently answers everything scores perfectly.

That last category is the one people leave out, and it is the one that catches
the failure mode that matters most in teaching: a fluent, confident, wrong
answer that a student cannot detect.

## Note on scoring

`expect_no_answer` cases are scored inverted — success is the *absence* of a
confident match. Mixing them into the same rule as the others would mark
correct behaviour as failure.
