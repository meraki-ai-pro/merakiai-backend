-- Allow 'application' in document_chunks.mode.
--
-- The application's mode vocabulary is (learn, review, application):
--   documents.default_mode  CHECK ... ('learn','review','application')
--   conversations.mode      CHECK ... ('learn','practice','review','application')
--   sessions.current_mode   uses 'application'
--   Pinecone namespaces     "{course_id}-application[-v2]"
--
-- document_chunks.mode was left on an older vocabulary that named the mode
-- after the document type ('practice') and never gained 'application'. Any
-- ingestion of a practice/application document therefore fails at the chunk
-- bookkeeping insert, after its vectors have already been written to Pinecone
-- — leaving the vectors live but the document marked failed.
--
-- Widening rather than replacing: 'practice' stays valid, so existing rows
-- (e.g. those in the froth-practice namespace) remain conformant and no
-- backfill is required.

ALTER TABLE public.document_chunks
  DROP CONSTRAINT IF EXISTS document_chunks_mode_check;

ALTER TABLE public.document_chunks
  ADD CONSTRAINT document_chunks_mode_check
  CHECK (mode = ANY (ARRAY['learn'::text, 'review'::text, 'practice'::text, 'application'::text]));

-- Verify:
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid = 'public.document_chunks'::regclass
--     AND conname = 'document_chunks_mode_check';
