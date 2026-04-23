-- =============================================================================
-- Migration 001 — Analytics tables and columns
-- Run this in the Supabase SQL editor (Dashboard → SQL Editor → New query)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. platform_sessions
--    One row per login event.
--
--    Useful queries:
--      Login count per user:
--        SELECT user_id, COUNT(*) AS login_count
--        FROM platform_sessions GROUP BY user_id ORDER BY login_count DESC;
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform_sessions (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    login_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_platform_sessions_user
    ON platform_sessions (user_id);


-- -----------------------------------------------------------------------------
-- 2. ws_sessions
--    One row per WebSocket connection (= one active usage window).
--
--    Useful queries:
--      Total active time per user:
--        SELECT user_id,
--               SUM(duration_sec)                       AS total_active_sec,
--               ROUND(SUM(duration_sec) / 60.0, 1)     AS total_active_min,
--               COUNT(*)                                AS session_count
--        FROM ws_sessions WHERE duration_sec IS NOT NULL
--        GROUP BY user_id;
--
--      Average session length:
--        SELECT AVG(duration_sec) AS avg_sec FROM ws_sessions
--        WHERE duration_sec IS NOT NULL;
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ws_sessions (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    session_id       UUID        NOT NULL,
    connected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disconnected_at  TIMESTAMPTZ,
    duration_sec     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_ws_sessions_user
    ON ws_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_ws_sessions_session
    ON ws_sessions (session_id);


-- -----------------------------------------------------------------------------
-- 3. request_metrics — add timing-breakdown columns
--
--    Useful queries:
--      Text vs video average response time:
--        SELECT response_format,
--               ROUND(AVG(ai_processing_ms))      AS avg_ai_ms,
--               ROUND(AVG(video_generation_ms))   AS avg_video_ms,
--               ROUND(AVG(processing_time_sec*1000)) AS avg_total_ms,
--               COUNT(*)                          AS requests
--        FROM request_metrics GROUP BY response_format;
--
--      Mode popularity + avg speed:
--        SELECT mode,
--               COUNT(*)                          AS usage_count,
--               ROUND(AVG(ai_processing_ms))      AS avg_ai_ms
--        FROM request_metrics GROUP BY mode ORDER BY usage_count DESC;
-- -----------------------------------------------------------------------------
ALTER TABLE request_metrics
    ADD COLUMN IF NOT EXISTS mode                 TEXT,
    ADD COLUMN IF NOT EXISTS response_format      TEXT    DEFAULT 'text',
    ADD COLUMN IF NOT EXISTS ai_processing_ms     INTEGER,
    ADD COLUMN IF NOT EXISTS video_generation_ms  INTEGER;


-- -----------------------------------------------------------------------------
-- 4. mode_sessions — add duration and message count
--
--    Useful queries:
--      Time per mode per user:
--        SELECT user_id, mode,
--               SUM(duration_sec)   AS total_sec,
--               COUNT(*)            AS sessions,
--               AVG(message_count)  AS avg_messages
--        FROM mode_sessions WHERE duration_sec IS NOT NULL
--        GROUP BY user_id, mode;
--
--      Difficulty progression (review):
--        SELECT difficulty, COUNT(*) AS count
--        FROM mode_sessions WHERE mode = 'review'
--        GROUP BY difficulty ORDER BY count DESC;
-- -----------------------------------------------------------------------------
ALTER TABLE mode_sessions
    ADD COLUMN IF NOT EXISTS duration_sec   INTEGER,
    ADD COLUMN IF NOT EXISTS message_count  INTEGER DEFAULT 0;


-- -----------------------------------------------------------------------------
-- 5. Useful cross-table views (optional, for dashboard/reporting)
-- -----------------------------------------------------------------------------

-- Per-user engagement summary
CREATE OR REPLACE VIEW user_engagement_summary AS
SELECT
    u.id                                                        AS user_id,
    u.email,
    COUNT(DISTINCT ps.id)                                       AS login_count,
    COALESCE(SUM(ws.duration_sec), 0)                           AS total_active_sec,
    ROUND(COALESCE(SUM(ws.duration_sec), 0) / 60.0, 1)         AS total_active_min,
    COUNT(DISTINCT ws.id)                                       AS ws_session_count
FROM auth.users u
LEFT JOIN platform_sessions ps ON ps.user_id = u.id
LEFT JOIN ws_sessions        ws ON ws.user_id = u.id AND ws.duration_sec IS NOT NULL
GROUP BY u.id, u.email;


-- Per-user mode breakdown
CREATE OR REPLACE VIEW user_mode_breakdown AS
SELECT
    user_id,
    mode,
    COUNT(*)            AS sessions,
    SUM(duration_sec)   AS total_sec,
    AVG(message_count)  AS avg_messages,
    AVG(
        CASE WHEN difficulty = 'Basic'        THEN 1
             WHEN difficulty = 'Intermediate' THEN 2
             WHEN difficulty = 'Advanced'     THEN 3
             ELSE NULL END
    )                   AS avg_difficulty_score
FROM mode_sessions
WHERE duration_sec IS NOT NULL
GROUP BY user_id, mode;


-- Response format and speed overview
CREATE OR REPLACE VIEW response_performance AS
SELECT
    mode,
    response_format,
    COUNT(*)                            AS requests,
    ROUND(AVG(ai_processing_ms))        AS avg_ai_ms,
    ROUND(AVG(video_generation_ms))     AS avg_video_ms,
    ROUND(AVG(processing_time_sec * 1000)) AS avg_total_ms,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ai_processing_ms)) AS p95_ai_ms
FROM request_metrics
WHERE ai_processing_ms IS NOT NULL
GROUP BY mode, response_format
ORDER BY mode, response_format;
