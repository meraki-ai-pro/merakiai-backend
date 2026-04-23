-- ============================================================
-- RLS Gap-Fill + Indexes Migration
-- Run in Supabase SQL Editor (Dashboard → SQL Editor)
--
-- This script ONLY adds what is missing given the policies
-- already present. Nothing here duplicates existing policies.
--
-- Existing policies already cover:
--   conversations  → select (own + admin), insert
--   sessions       → select (own + admin), insert, update
--   users          → select (own + admin), update, super_admin mgmt
--   mode_feedback  → select (own + admin), insert
--   session_surveys→ select (own + admin), insert
--   user_feedback  → select (own + admin), insert
--   documents      → admin ALL
--   document_chunks→ admin ALL
--   content_quality_flags / daily_platform_metrics / engagement_kpis /
--   learning_outcome_kpis / mode_kpis → admin read-only
--
-- What this script adds:
--   1. is_admin() helper function
--   2. Enable RLS on tables that have policies but may not have it on
--   3. Full policies for mode_sessions, session_state, review_attempts,
--      review_summaries, request_metrics, platform_sessions, ws_sessions
--   4. Public read policies for courses, avatar_voice_bundles
--   5. RLS enable (deny-all) for embedding_cache
--   6. Indexes on all FK / filter columns
-- ============================================================


-- ============================================================
-- SECTION 0: Admin helper (SECURITY DEFINER avoids recursion)
-- ============================================================

CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = public
SET row_security = off        -- prevents recursion when users table has RLS + is_admin() policies
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.users
    WHERE id = auth.uid()
    AND role IN ('admin', 'super_admin')
  )
$$;


-- ============================================================
-- SECTION 1: Enable RLS on tables that already have policies
-- (safe to run even if already enabled)
-- ============================================================

ALTER TABLE public.users             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversations     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sessions          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mode_feedback     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.session_surveys   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_feedback     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documents         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document_chunks   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_quality_flags   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_platform_metrics  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.engagement_kpis         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.learning_outcome_kpis   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mode_kpis               ENABLE ROW LEVEL SECURITY;


-- ============================================================
-- SECTION 2: Tables with NO existing policies (full setup)
-- ============================================================

-- ── mode_sessions ──────────────────────────────────────────────
ALTER TABLE public.mode_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "mode_sessions_select_own" ON public.mode_sessions
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "mode_sessions_select_admin" ON public.mode_sessions
  FOR SELECT USING (public.is_admin());

CREATE POLICY "mode_sessions_insert_own" ON public.mode_sessions
  FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Celery tasks update completed/ended_at/current_item via service role (bypasses RLS).
-- This policy covers any future direct user updates.
CREATE POLICY "mode_sessions_update_own" ON public.mode_sessions
  FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);


-- ── session_state ─────────────────────────────────────────────
-- No user_id — scoped through the parent mode_session
ALTER TABLE public.session_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "session_state_own_or_admin" ON public.session_state
  FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM public.mode_sessions ms
      WHERE ms.id = session_state.mode_session_id
        AND ms.user_id = auth.uid()
    )
    OR public.is_admin()
  )
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM public.mode_sessions ms
      WHERE ms.id = session_state.mode_session_id
        AND ms.user_id = auth.uid()
    )
    OR public.is_admin()
  );


-- ── review_attempts ────────────────────────────────────────────
ALTER TABLE public.review_attempts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "review_attempts_select_own" ON public.review_attempts
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "review_attempts_select_admin" ON public.review_attempts
  FOR SELECT USING (public.is_admin());

CREATE POLICY "review_attempts_insert_own" ON public.review_attempts
  FOR INSERT WITH CHECK (auth.uid() = user_id);


-- ── review_summaries ───────────────────────────────────────────
ALTER TABLE public.review_summaries ENABLE ROW LEVEL SECURITY;

CREATE POLICY "review_summaries_select_own" ON public.review_summaries
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "review_summaries_select_admin" ON public.review_summaries
  FOR SELECT USING (public.is_admin());

CREATE POLICY "review_summaries_insert_own" ON public.review_summaries
  FOR INSERT WITH CHECK (auth.uid() = user_id);


-- ── request_metrics ────────────────────────────────────────────
-- Inserted by Celery via service role (bypasses RLS).
-- INSERT policy is here for defence-in-depth if ever switched to user JWT.
ALTER TABLE public.request_metrics ENABLE ROW LEVEL SECURITY;

CREATE POLICY "request_metrics_insert_own" ON public.request_metrics
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "request_metrics_select_admin" ON public.request_metrics
  FOR SELECT USING (public.is_admin());


-- ── platform_sessions ──────────────────────────────────────────
ALTER TABLE public.platform_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "platform_sessions_insert_own" ON public.platform_sessions
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "platform_sessions_select_admin" ON public.platform_sessions
  FOR SELECT USING (public.is_admin());


-- ── ws_sessions ────────────────────────────────────────────────
ALTER TABLE public.ws_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "ws_sessions_insert_own" ON public.ws_sessions
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "ws_sessions_update_own" ON public.ws_sessions
  FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

CREATE POLICY "ws_sessions_select_own_or_admin" ON public.ws_sessions
  FOR SELECT USING (auth.uid() = user_id OR public.is_admin());


-- ── courses ────────────────────────────────────────────────────
-- No existing policies; all authenticated users need to list courses
ALTER TABLE public.courses ENABLE ROW LEVEL SECURITY;

CREATE POLICY "courses_select_authenticated" ON public.courses
  FOR SELECT USING (auth.uid() IS NOT NULL);

CREATE POLICY "courses_admin_write" ON public.courses
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());


-- ── avatar_voice_bundles ───────────────────────────────────────
ALTER TABLE public.avatar_voice_bundles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "avatar_voice_bundles_select_authenticated" ON public.avatar_voice_bundles
  FOR SELECT USING (auth.uid() IS NOT NULL);

CREATE POLICY "avatar_voice_bundles_admin_write" ON public.avatar_voice_bundles
  FOR ALL USING (public.is_admin()) WITH CHECK (public.is_admin());


-- ── embedding_cache ────────────────────────────────────────────
-- Service role (Celery) bypasses RLS and can always access this.
-- No policies = deny all for anon/authenticated requests.
ALTER TABLE public.embedding_cache ENABLE ROW LEVEL SECURITY;


-- ============================================================
-- SECTION 3: Indexes
-- ============================================================

-- sessions
CREATE INDEX IF NOT EXISTS idx_sessions_user_id   ON public.sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_course_id ON public.sessions(course_id);

-- conversations
CREATE INDEX IF NOT EXISTS idx_conversations_session_id ON public.conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_user_id    ON public.conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON public.conversations(created_at DESC);

-- mode_sessions
CREATE INDEX IF NOT EXISTS idx_mode_sessions_session_id ON public.mode_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_mode_sessions_user_id    ON public.mode_sessions(user_id);

-- session_state
CREATE INDEX IF NOT EXISTS idx_session_state_session_id ON public.session_state(session_id);

-- review_attempts / summaries
CREATE INDEX IF NOT EXISTS idx_review_attempts_session_id  ON public.review_attempts(session_id);
CREATE INDEX IF NOT EXISTS idx_review_attempts_user_id     ON public.review_attempts(user_id);
CREATE INDEX IF NOT EXISTS idx_review_summaries_session_id ON public.review_summaries(session_id);
CREATE INDEX IF NOT EXISTS idx_review_summaries_user_id    ON public.review_summaries(user_id);

-- feedback tables
CREATE INDEX IF NOT EXISTS idx_mode_feedback_session_id   ON public.mode_feedback(session_id);
CREATE INDEX IF NOT EXISTS idx_mode_feedback_user_id      ON public.mode_feedback(user_id);
CREATE INDEX IF NOT EXISTS idx_session_surveys_session_id ON public.session_surveys(session_id);
CREATE INDEX IF NOT EXISTS idx_session_surveys_user_id    ON public.session_surveys(user_id);
CREATE INDEX IF NOT EXISTS idx_user_feedback_user_id      ON public.user_feedback(user_id);

-- analytics
CREATE INDEX IF NOT EXISTS idx_request_metrics_session_id ON public.request_metrics(session_id);
CREATE INDEX IF NOT EXISTS idx_request_metrics_user_id    ON public.request_metrics(user_id);
CREATE INDEX IF NOT EXISTS idx_platform_sessions_user_id  ON public.platform_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_ws_sessions_user_id        ON public.ws_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_ws_sessions_session_id     ON public.ws_sessions(session_id);

-- documents / chunks
CREATE INDEX IF NOT EXISTS idx_documents_course_id        ON public.documents(course_id);
CREATE INDEX IF NOT EXISTS idx_documents_status           ON public.documents(status);
CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON public.document_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_namespace  ON public.document_chunks(pinecone_namespace);
CREATE INDEX IF NOT EXISTS idx_document_chunks_mode       ON public.document_chunks(mode);
