-- WARNING: This schema is for context only and is not meant to be run.
-- Table order and constraints may not be valid for execution.

CREATE TABLE public.avatar_voice_bundles (
  avatar_id text NOT NULL,
  avatar_gender text NOT NULL CHECK (avatar_gender = ANY (ARRAY['male'::text, 'female'::text])),
  avatar_provider text NOT NULL DEFAULT 'd-id'::text,
  voice_id text NOT NULL,
  voice_gender text NOT NULL CHECK (voice_gender = ANY (ARRAY['male'::text, 'female'::text])),
  voice_provider text NOT NULL DEFAULT 'elevenlabs'::text,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  did_presenter_id text NOT NULL,
  CONSTRAINT avatar_voice_bundles_pkey PRIMARY KEY (avatar_id)
);
CREATE TABLE public.content_quality_flags (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  mode text NOT NULL CHECK (mode = ANY (ARRAY['learn'::text, 'practice'::text, 'review'::text])),
  issue_type text NOT NULL CHECK (issue_type = ANY (ARRAY['confusing'::text, 'too_fast'::text, 'too_complex'::text, 'incorrect'::text, 'other'::text])),
  frequency integer NOT NULL,
  detected_on date NOT NULL,
  CONSTRAINT content_quality_flags_pkey PRIMARY KEY (id)
);
CREATE TABLE public.conversations (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  user_id uuid NOT NULL,
  mode text NOT NULL CHECK (mode = ANY (ARRAY['learn'::text, 'practice'::text, 'review'::text])),
  user_input text NOT NULL,
  tutor_response text NOT NULL,
  response_format text NOT NULL CHECK (response_format = ANY (ARRAY['text'::text, 'video'::text])),
  created_at timestamp with time zone DEFAULT now(),
  video_url text,
  audio_url text,
  CONSTRAINT conversations_pkey PRIMARY KEY (id),
  CONSTRAINT conversations_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id),
  CONSTRAINT conversations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.courses (
  id text NOT NULL,
  name text NOT NULL,
  description text,
  persona text,
  domain_topics ARRAY DEFAULT '{}'::text[],
  difficulty_descriptors jsonb DEFAULT '{}'::jsonb,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT courses_pkey PRIMARY KEY (id)
);
CREATE TABLE public.daily_platform_metrics (
  metric_date date NOT NULL,
  total_users integer NOT NULL,
  active_users integer NOT NULL,
  sessions_started integer NOT NULL,
  sessions_completed integer NOT NULL,
  reviews_completed integer NOT NULL,
  avg_overall_rating numeric,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT daily_platform_metrics_pkey PRIMARY KEY (metric_date)
);
CREATE TABLE public.document_chunks (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL,
  pinecone_id text NOT NULL,
  pinecone_namespace text NOT NULL,
  mode text NOT NULL CHECK (mode = ANY (ARRAY['learn'::text, 'review'::text, 'practice'::text])),
  topic text,
  difficulty text,
  content_hash text NOT NULL,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT document_chunks_pkey PRIMARY KEY (id),
  CONSTRAINT document_chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id)
);
CREATE TABLE public.documents (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  title text NOT NULL,
  source_filename text NOT NULL,
  doc_type text NOT NULL CHECK (doc_type = ANY (ARRAY['knowledge'::text, 'assessment'::text, 'practice'::text])),
  default_mode text NOT NULL CHECK (default_mode = ANY (ARRAY['learn'::text, 'review'::text, 'application'::text])),
  difficulty text NOT NULL CHECK (difficulty = ANY (ARRAY['basic'::text, 'intermediate'::text, 'advanced'::text])),
  version text NOT NULL,
  status text NOT NULL DEFAULT 'processing'::text CHECK (status = ANY (ARRAY['processing'::text, 'ready'::text, 'failed'::text])),
  total_chunks integer DEFAULT 0,
  created_by uuid,
  created_at timestamp with time zone DEFAULT now(),
  updated_at timestamp with time zone DEFAULT now(),
  course_id text,
  CONSTRAINT documents_pkey PRIMARY KEY (id),
  CONSTRAINT documents_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id),
  CONSTRAINT documents_created_by_fkey FOREIGN KEY (created_by) REFERENCES auth.users(id)
);
CREATE TABLE public.embedding_cache (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  text_hash text NOT NULL UNIQUE,
  embedding USER-DEFINED,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT embedding_cache_pkey PRIMARY KEY (id)
);
CREATE TABLE public.engagement_kpis (
  metric_date date NOT NULL,
  new_users integer NOT NULL,
  returning_users integer NOT NULL,
  avg_sessions_per_user numeric,
  avg_session_duration_minutes numeric,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT engagement_kpis_pkey PRIMARY KEY (metric_date)
);
CREATE TABLE public.learning_outcome_kpis (
  metric_date date NOT NULL,
  avg_review_score numeric,
  pass_rate numeric,
  avg_confidence_rating numeric,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT learning_outcome_kpis_pkey PRIMARY KEY (metric_date)
);
CREATE TABLE public.mode_feedback (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  user_id uuid NOT NULL,
  mode text NOT NULL CHECK (mode = ANY (ARRAY['learn'::text, 'practice'::text, 'review'::text])),
  ease_of_understanding integer CHECK (ease_of_understanding >= 1 AND ease_of_understanding <= 5),
  engagement_level integer CHECK (engagement_level >= 1 AND engagement_level <= 5),
  usefulness integer CHECK (usefulness >= 1 AND usefulness <= 5),
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT mode_feedback_pkey PRIMARY KEY (id),
  CONSTRAINT mode_feedback_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id),
  CONSTRAINT mode_feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.mode_kpis (
  metric_date date NOT NULL,
  mode text NOT NULL CHECK (mode = ANY (ARRAY['learn'::text, 'practice'::text, 'review'::text])),
  total_sessions integer NOT NULL,
  avg_engagement numeric,
  avg_usefulness numeric,
  CONSTRAINT mode_kpis_pkey PRIMARY KEY (metric_date, mode)
);
CREATE TABLE public.mode_sessions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  user_id uuid NOT NULL,
  mode text NOT NULL CHECK (mode = ANY (ARRAY['practice'::text, 'review'::text])),
  session_type text NOT NULL,
  difficulty text CHECK (difficulty = ANY (ARRAY['Basic'::text, 'Intermediate'::text, 'Advanced'::text])),
  completed boolean DEFAULT false,
  started_at timestamp with time zone DEFAULT now(),
  ended_at timestamp with time zone,
  total_items integer NOT NULL DEFAULT 10,
  current_item integer NOT NULL DEFAULT 1,
  duration_sec integer,
  message_count integer DEFAULT 0,
  CONSTRAINT mode_sessions_pkey PRIMARY KEY (id),
  CONSTRAINT mode_sessions_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id),
  CONSTRAINT mode_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.platform_sessions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  login_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT platform_sessions_pkey PRIMARY KEY (id),
  CONSTRAINT platform_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id)
);
CREATE TABLE public.request_metrics (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  user_id uuid NOT NULL,
  processing_time_sec numeric NOT NULL,
  task_type text NOT NULL,
  created_at timestamp with time zone DEFAULT now(),
  mode text,
  response_format text DEFAULT 'text'::text,
  ai_processing_ms integer,
  video_generation_ms integer,
  CONSTRAINT request_metrics_pkey PRIMARY KEY (id)
);
CREATE TABLE public.review_attempts (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  user_id uuid NOT NULL,
  question text NOT NULL,
  user_answer text NOT NULL,
  verdict text NOT NULL CHECK (verdict = ANY (ARRAY['correct'::text, 'partially_correct'::text, 'incorrect'::text])),
  score integer NOT NULL CHECK (score >= 0 AND score <= 5),
  feedback text NOT NULL,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT review_attempts_pkey PRIMARY KEY (id),
  CONSTRAINT review_attempts_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id),
  CONSTRAINT review_attempts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.review_summaries (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  user_id uuid NOT NULL,
  overall_score integer CHECK (overall_score >= 0 AND overall_score <= 5),
  strengths text NOT NULL,
  weaknesses text NOT NULL,
  recommendations text NOT NULL,
  generated_at timestamp with time zone DEFAULT now(),
  CONSTRAINT review_summaries_pkey PRIMARY KEY (id),
  CONSTRAINT review_summaries_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id),
  CONSTRAINT review_summaries_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.session_state (
  session_id uuid NOT NULL,
  pending_mode text CHECK (pending_mode = ANY (ARRAY['practice'::text, 'review'::text])),
  pending_payload jsonb,
  step integer NOT NULL DEFAULT 1,
  total_steps integer NOT NULL DEFAULT 1,
  difficulty text,
  contexts jsonb,
  updated_at timestamp with time zone DEFAULT now(),
  mode_session_id uuid NOT NULL,
  CONSTRAINT session_state_pkey PRIMARY KEY (mode_session_id)
);
CREATE TABLE public.session_surveys (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL,
  user_id uuid NOT NULL,
  clarity_rating integer NOT NULL CHECK (clarity_rating >= 1 AND clarity_rating <= 5),
  helpfulness_rating integer NOT NULL CHECK (helpfulness_rating >= 1 AND helpfulness_rating <= 5),
  confidence_rating integer NOT NULL CHECK (confidence_rating >= 1 AND confidence_rating <= 5),
  overall_rating integer NOT NULL CHECK (overall_rating >= 1 AND overall_rating <= 5),
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT session_surveys_pkey PRIMARY KEY (id),
  CONSTRAINT session_surveys_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id),
  CONSTRAINT session_surveys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)
);
CREATE TABLE public.sessions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id uuid,
  current_mode text NOT NULL DEFAULT 'learn'::text CHECK (current_mode = ANY (ARRAY['learn'::text, 'practice'::text, 'review'::text])),
  prefers_video boolean DEFAULT false,
  started_at timestamp with time zone DEFAULT now(),
  ended_at timestamp with time zone,
  course_id text,
  CONSTRAINT sessions_pkey PRIMARY KEY (id),
  CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id),
  CONSTRAINT sessions_course_id_fkey FOREIGN KEY (course_id) REFERENCES public.courses(id)
);
CREATE TABLE public.user_feedback (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  session_id uuid,
  feedback_type text NOT NULL CHECK (feedback_type = ANY (ARRAY['bug'::text, 'suggestion'::text, 'content'::text, 'ux'::text, 'other'::text])),
  message text NOT NULL,
  created_at timestamp with time zone DEFAULT now(),
  CONSTRAINT user_feedback_pkey PRIMARY KEY (id),
  CONSTRAINT user_feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id),
  CONSTRAINT user_feedback_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id)
);
CREATE TABLE public.users (
  id uuid NOT NULL,
  email text NOT NULL UNIQUE,
  role text NOT NULL DEFAULT 'user'::text CHECK (role = ANY (ARRAY['user'::text, 'admin'::text, 'super_admin'::text])),
  avatar_id text NOT NULL,
  avatar_provider text NOT NULL DEFAULT 'd-id'::text,
  created_at timestamp with time zone DEFAULT now(),
  avatar_gender text CHECK (avatar_gender = ANY (ARRAY['male'::text, 'female'::text])),
  voice_provider text NOT NULL DEFAULT 'elevenlabs'::text,
  voice_id text,
  voice_gender text CHECK (voice_gender = ANY (ARRAY['male'::text, 'female'::text])),
  first_name text,
  last_name text,
  university_name text,
  region text,
  country text,
  profile_picture_url text,
  CONSTRAINT users_pkey PRIMARY KEY (id),
  CONSTRAINT users_id_fkey FOREIGN KEY (id) REFERENCES auth.users(id)
);
CREATE TABLE public.ws_sessions (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  session_id uuid NOT NULL,
  connected_at timestamp with time zone NOT NULL DEFAULT now(),
  disconnected_at timestamp with time zone,
  duration_sec integer,
  CONSTRAINT ws_sessions_pkey PRIMARY KEY (id),
  CONSTRAINT ws_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id)
);