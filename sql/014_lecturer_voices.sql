-- Lecturer voice cloning, tied to the courses they own.
--
-- A lecturer records a short sample, ElevenLabs clones it, and that voice
-- reads both the concept videos and the Learn-mode lesson board for their
-- course. A student on Calculus hears their own Calculus lecturer; a student
-- on a course whose lecturer has not recorded anything hears a clear default.
--
-- Why a table rather than a column on `courses`:
--
--   * a voice is an object with a life cycle — created at ElevenLabs, usable,
--     replaced, deleted — and deleting it there must be driven from a row we
--     still hold;
--   * one lecturer teaching four courses records ONCE and attaches the same
--     voice to all four, which a per-course column cannot express without
--     duplicating the provider id four times and orphaning three of them on
--     deletion.
--
-- The provider id is stored, not the audio. ElevenLabs holds the model; the
-- sample is used to create it and then discarded, so this table never becomes
-- a store of biometric voice data.
--
-- PREREQUISITES: 003 (owns_course/is_admin), 004 (touch_updated_at).
--
-- Idempotent: safe to run more than once.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Voices
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.lecturer_voices (
  id                uuid        NOT NULL DEFAULT gen_random_uuid(),
  owner_id          uuid        NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  name              text        NOT NULL,

  provider          text        NOT NULL DEFAULT 'elevenlabs'
                                CHECK (provider = ANY (ARRAY['elevenlabs'::text])),
  -- The cloned voice at the provider. Nullable while a clone is in flight so
  -- a failed creation still leaves a row explaining what happened.
  provider_voice_id text,

  status            text        NOT NULL DEFAULT 'pending'
                                CHECK (status = ANY (ARRAY[
                                  'pending'::text, 'ready'::text, 'failed'::text
                                ])),
  error             text,

  -- How much audio the clone was built from. Shown to the lecturer, because
  -- the honest answer to "why does this not sound like me" is usually "you
  -- recorded eleven seconds".
  sample_seconds    numeric,

  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  deleted_at        timestamptz,

  CONSTRAINT lecturer_voices_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS lecturer_voices_owner_idx
  ON public.lecturer_voices (owner_id)
  WHERE deleted_at IS NULL;

DROP TRIGGER IF EXISTS lecturer_voices_touch_updated_at ON public.lecturer_voices;
CREATE TRIGGER lecturer_voices_touch_updated_at
  BEFORE UPDATE ON public.lecturer_voices
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

ALTER TABLE public.lecturer_voices ENABLE ROW LEVEL SECURITY;

-- A lecturer sees and manages only their own voices.
DROP POLICY IF EXISTS lecturer_voices_own_all ON public.lecturer_voices;
CREATE POLICY lecturer_voices_own_all ON public.lecturer_voices
  FOR ALL
  USING (owner_id = auth.uid())
  WITH CHECK (owner_id = auth.uid());

DROP POLICY IF EXISTS lecturer_voices_admin_all ON public.lecturer_voices;
CREATE POLICY lecturer_voices_admin_all ON public.lecturer_voices
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());

-- Deliberately NO student policy. A student never reads this table: the server
-- resolves a course's voice with the service role and hands back audio, never
-- a provider voice id. A leaked provider id is a voice anyone with our API key
-- could speak as.


-- ---------------------------------------------------------------------------
-- 2. Attach a voice to a course
-- ---------------------------------------------------------------------------
-- ON DELETE SET NULL, not CASCADE: deleting a voice must fall the course back
-- to the default narrator, never delete the course.

ALTER TABLE public.courses
  ADD COLUMN IF NOT EXISTS lecturer_voice_id uuid
  REFERENCES public.lecturer_voices(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS courses_lecturer_voice_idx
  ON public.courses (lecturer_voice_id)
  WHERE lecturer_voice_id IS NOT NULL;

COMMIT;


-- Verify:
--   SELECT column_name FROM information_schema.columns
--    WHERE table_name = 'lecturer_voices' ORDER BY ordinal_position;
--
--   SELECT id, name, lecturer_voice_id FROM public.courses;
--
--   SELECT polname FROM pg_policy
--    WHERE polrelid = 'public.lecturer_voices'::regclass;
