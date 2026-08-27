-- 015: Accept PowerPoint decks as knowledge files.
--
-- Lecturers teach from slides. The parser, the upload allowlist and the UI all
-- accept .pptx now; this is the last gate, and it is the one that fails
-- quietly. Storage rejects an unlisted MIME type with 415, and ingestion
-- catches that and carries on:
--
--   "Could not retain source file for document <id>; ingestion continues but
--    re-ingestion will need the original"
--
-- So a deck uploaded before this migration is parsed, chunked, embedded and
-- fully answerable — but the original file is gone. Re-ingestion (a re-chunk
-- after a parser improvement, say) has nothing to read, and the lecturer is
-- asked to upload a file the UI already lists as present.
--
-- 007 created the bucket with ON CONFLICT DO NOTHING, so re-running it fixes
-- nothing on an existing database. This UPDATEs instead.

BEGIN;

UPDATE storage.buckets
SET allowed_mime_types = ARRAY[
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/msword',
  -- .pptx. The legacy binary .ppt is deliberately absent: python-pptx cannot
  -- read that container, so accepting it would store a file that ingestion
  -- then fails on.
  'application/vnd.openxmlformats-officedocument.presentationml.presentation'
]
WHERE id = 'course-documents';

COMMIT;


-- Verify:
--   SELECT id, allowed_mime_types FROM storage.buckets WHERE id = 'course-documents';
--
-- Expect four entries, including ...presentationml.presentation
--
-- Decks uploaded before this ran keep working for retrieval; only their source
-- file is missing. To find them:
--   SELECT id, filename FROM documents
--   WHERE filename ILIKE '%.pptx' AND storage_path IS NULL;
