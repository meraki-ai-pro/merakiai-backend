-- Persist retrieval sources, and keep the files behind them.
--
-- Two related gaps:
--
--   1. Sources are pushed over the WebSocket for the live turn only. Reload a
--      conversation and the answer still shows [1][2] markers, but they are
--      inert text with no drawer behind them — the citation UI silently
--      degrades to noise on every page refresh.
--
--   2. Ingestion reads the upload into memory, parses it and drops it. The
--      storage bucket holds audio, logos and profile pictures — never the
--      lecturer's actual course material. Re-ingestion, file versioning and
--      split-view source highlighting all require the original bytes.
--
-- Ref: TECHNICAL_DOCUMENTATION §25.1
--      Meraki_AI_Lecturer_Side_Technical_Documentation §3.3, §4.2
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Conversation columns
-- ---------------------------------------------------------------------------
-- jsonb, not a join table. These are an immutable snapshot of what the answer
-- was grounded in *at the time it was written* — a normalised reference would
-- start lying the moment a document is re-ingested or unpublished, and the
-- whole point of a citation is that it records what was actually used.

ALTER TABLE public.conversations
  ADD COLUMN IF NOT EXISTS sources jsonb;

-- Student-submitted images for the turn (photographed handwritten work).
-- Without this a reloaded conversation shows feedback about a photo that is
-- no longer on screen.
ALTER TABLE public.conversations
  ADD COLUMN IF NOT EXISTS attachments jsonb;


-- ---------------------------------------------------------------------------
-- 2. Storage buckets
-- ---------------------------------------------------------------------------
-- Both PRIVATE, unlike the three existing buckets.
--
--   course-documents — the lecturer's own teaching notes and past papers.
--                      Publishing these at a guessable public URL would put
--                      copyrighted course material on the open web.
--   student-uploads  — photographs of a named student's work. Personal data
--                      under any reading of the pilot's consent process.
--
-- Access is via short-lived signed URLs minted by the backend, which uses the
-- service role. RLS on storage.objects therefore needs no policy for these
-- buckets: no policy means no direct access for anon or authenticated roles,
-- which is exactly right. Do not add one without a reason.

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
  'course-documents',
  'course-documents',
  false,
  52428800,  -- 50 MB, matching the ingestion upload cap
  ARRAY[
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword'
  ]
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
  'student-uploads',
  'student-uploads',
  false,
  3670016,  -- 3.5 MB, matching MAX_IMAGE_BYTES in app/media/image_input.py
  ARRAY['image/jpeg', 'image/png', 'image/gif', 'image/webp']
)
ON CONFLICT (id) DO NOTHING;

COMMIT;


-- Verify:
--   SELECT id, public, file_size_limit FROM storage.buckets
--   WHERE id IN ('course-documents','student-uploads');
--
--   SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name = 'conversations' AND column_name IN ('sources','attachments');
--
--   -- expect zero rows: these buckets are reached only via signed URLs
--   SELECT polname FROM pg_policy
--   WHERE polrelid = 'storage.objects'::regclass
--     AND pg_get_expr(polqual, polrelid) LIKE '%course-documents%';
