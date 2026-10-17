-- 023: Lecture videos as course sources (after 006 and 007).
--
-- A lecturer can add a lecture video, by upload or by link, as course
-- material. Its speech is transcribed (ElevenLabs speech-to-text), indexed like
-- any document, and a student's source opens the video at the moment cited.
--
-- * course-videos: private bucket. The browser uploads straight into it with a
--   signed upload URL, so the API's 50 MB request cap does not apply; the cap
--   here is 1 GB per file. The project's GLOBAL upload limit (Dashboard >
--   Storage > Settings) must be at least this for large files to go through.
-- * documents.media_url: the link, for a video added by URL rather than upload.
--
-- Idempotent.

BEGIN;

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
  'course-videos', 'course-videos', false, 1073741824,
  ARRAY['video/mp4', 'video/webm', 'video/quicktime']
)
ON CONFLICT (id) DO UPDATE SET
  public = false,
  file_size_limit = EXCLUDED.file_size_limit,
  allowed_mime_types = EXCLUDED.allowed_mime_types;

ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS media_url text;

COMMIT;
