-- =============================================================================
-- Migration 002 — Fix documents.default_mode check constraint
-- The original constraint did not include 'application' as a valid value.
-- The code maps doc_type='practice' -> default_mode='application'.
-- Run this in the Supabase SQL editor.
-- =============================================================================

-- Step 1: Drop the old constraint (already done if you ran the previous version)
ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_default_mode_check;

-- Step 2: Normalise any legacy values that were inserted before this migration.
--   'assess'   was an old alias for review mode
--   'practice' was an old alias for application mode
UPDATE documents SET default_mode = 'review'      WHERE default_mode = 'assess';
UPDATE documents SET default_mode = 'application' WHERE default_mode = 'practice';

-- Step 3: Add the corrected constraint
ALTER TABLE documents
    ADD CONSTRAINT documents_default_mode_check
    CHECK (default_mode IN ('learn', 'review', 'application'));
