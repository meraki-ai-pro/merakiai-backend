-- ============================================================
-- Meraki AI — Clear all app data
-- Preserves: users, avatar_voice_bundles
-- Clears:    all session, learning, AI, and content tables
--
-- Run in: Supabase Dashboard → SQL Editor
-- WARNING: this is irreversible. Back up first if needed.
-- ============================================================

-- Disable triggers temporarily to avoid firing audit hooks mid-truncate
SET session_replication_role = replica;

TRUNCATE TABLE
    -- ── Learning / session data ──────────────────────────────
    conversations,
    review_summaries,
    session_surveys,
    review_attempts,
    session_state,
    mode_sessions,
    sessions,

    -- ── Feedback & analytics ─────────────────────────────────
    user_feedback,
    mode_feedback,
    request_metrics,
    content_quality_flags,

    -- ── Infrastructure / tracking ────────────────────────────
    platform_sessions,
    ws_sessions,

    -- ── AI / content ─────────────────────────────────────────
    document_chunks,
    documents,
    embedding_cache,
    courses

RESTART IDENTITY CASCADE;

-- Re-enable triggers
SET session_replication_role = DEFAULT;

-- Verify: these two tables must still have rows
SELECT 'users' AS tbl, COUNT(*) AS rows FROM users
UNION ALL
SELECT 'avatar_voice_bundles', COUNT(*) FROM avatar_voice_bundles;
