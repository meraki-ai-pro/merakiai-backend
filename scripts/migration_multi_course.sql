-- ============================================================
-- Multi-Course RAG Migration
-- Run this in your Supabase SQL editor
-- ============================================================

-- 1. courses table
CREATE TABLE IF NOT EXISTS courses (
    id                    TEXT PRIMARY KEY,        -- slug: "froth-flotation", "hydrometallurgy"
    name                  TEXT NOT NULL,           -- display: "Froth Flotation"
    description           TEXT,
    persona               TEXT,                    -- tutor persona injected into system prompt
    domain_topics         TEXT[]  DEFAULT '{}',    -- topic list for domain constraint
    difficulty_descriptors JSONB  DEFAULT '{}',    -- {"Basic": "...", "Intermediate": "...", "Advanced": "..."}
    created_at            TIMESTAMPTZ DEFAULT now()
);

-- 2. Add course_id to sessions (nullable so existing rows are unaffected)
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS course_id TEXT REFERENCES courses(id);

-- 3. Add course_id to documents so each ingested doc belongs to a course
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS course_id TEXT REFERENCES courses(id);

-- ============================================================
-- Seed: existing froth-flotation course
-- (keeps all current sessions and documents working)
-- ============================================================

INSERT INTO courses (id, name, description, persona, domain_topics, difficulty_descriptors)
VALUES (
    'froth-flotation',
    'Froth Flotation',
    'Mineral processing technique using surface chemistry to separate valuable minerals from gangue.',
    'You are an expert Froth Flotation tutor and instructor. You teach as a calm, patient, and friendly lecturer. Your goal is to make students deeply understand froth flotation, even if they have no technical background. You do not assume prior knowledge. You avoid unnecessary jargon. You explain ideas step-by-step using simple language. You are not an assistant. You are a teacher.',
    ARRAY['Froth flotation', 'Mineral processing', 'Ore beneficiation', 'Surface chemistry related to flotation'],
    '{
        "Basic":        "Single-concept questions covering foundational terminology, definitions, and direct recall of key principles.",
        "Intermediate": "Multi-concept questions requiring process reasoning, cause-and-effect understanding, and application of principles to standard scenarios.",
        "Advanced":     "Complex troubleshooting scenarios, process optimisation problems, and novel applications that require integrating multiple concepts."
    }'::jsonb
)
ON CONFLICT (id) DO NOTHING;

-- Backfill existing sessions to froth-flotation so nothing breaks
UPDATE sessions
SET course_id = 'froth-flotation'
WHERE course_id IS NULL;

-- Backfill existing documents to froth-flotation
UPDATE documents
SET course_id = 'froth-flotation'
WHERE course_id IS NULL;
