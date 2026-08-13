# Migrations

Apply in numeric order. The numbering is not cosmetic — later files call
functions and reference tables created by earlier ones, so running them out of
order fails with a missing-function error rather than doing something subtly
wrong.

```bash
for f in sql/0*.sql; do psql "$SUPABASE_DB_URL" -f "$f"; done
```

Every file is idempotent and wrapped in a transaction, so re-running the whole
set is safe.

| # | File | Adds |
|---|---|---|
| 001 | allow_application_mode_in_document_chunks | `'application'` in the `document_chunks.mode` check |
| 002 | add_profile_picture | `users.profile_picture_url` |
| 003 | add_lecturer_role_and_course_ownership | `lecturer` role, `courses.owner_id`, `owns_course()`, `is_admin()` |
| 004 | add_enrolments_and_invite_codes | `enrolments`, `invite_codes`, `redeem_invite_code()`, `is_enrolled()`, `touch_updated_at()` |
| 005 | backfill_enrolments_from_sessions | enrolments for accounts predating the table |
| 006 | add_mode_aware_publishable_documents | `documents.target_modes`, `is_published`, `storage_path` |
| 007 | add_conversation_sources_and_file_retention | `conversations.sources`, private storage buckets |
| 008 | add_media_assets_render_pipeline | `media_assets`, `rendered-media` bucket |
| 009 | add_audit_logs | `audit_logs` |
| 010 | add_events_mastery_assessments | `events`, `mastery_states`, assessment tables |
| 011 | add_feedback_and_lifecycle | `feedback_responses`, deletion lifecycle columns |
| 012 | use_ghanaian_academic_levels | Level 100–600 / HND vocabulary |

## Dependencies worth knowing

**003 before everything after it.** It defines `owns_course()` and `is_admin()`,
which almost every later RLS policy calls.

**004 before 008, 010 and 011.** They use `is_enrolled()` and the
`touch_updated_at()` trigger function that 004 creates.

**005 is a backfill, not a schema change.** Run it once, after 004, and only if
you have accounts that existed before enrolments did. Skipping it on a fresh
database is correct; skipping it on an existing one locks every current student
out, because `require_enrolment` fails closed.

**003 redefines `is_admin()` deliberately.** It is referenced by roughly thirty
RLS policies. If the existing definition happened to be `role <> 'user'` rather
than an explicit membership test, adding the `lecturer` role would silently
grant every lecturer full admin access. Restating it removes that dependency on
history.

## Not a migration

`clear_app_data.sql` is a destructive development utility and is deliberately
outside the numbered sequence. Do not run it against anything you care about.
