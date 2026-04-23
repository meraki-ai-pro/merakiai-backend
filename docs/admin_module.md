# Admin Module

## New files created

| File | What it does |
|------|-------------|
| `app/core/llm_config.py` | Runtime-overridable LLM config (reads `llm_config.json` at root, falls back to hardcoded defaults) |
| `app/api/v1/admin/analytics.py` | 5 analytics endpoints aggregating all feedback tables |
| `app/api/v1/admin/users.py` | User list, detail, role update, delete |
| `app/api/v1/admin/documents.py` | Document list, detail, metadata update, delete (with Pinecone vector cleanup) |
| `app/api/v1/admin/courses.py` | Full course CRUD |
| `app/api/v1/admin/llm.py` | LLM config read/write + usage stats |
| `app/api/v1/admin/router.py` | Mounts all sub-routers under `/admin` |

## Modified files

- **`app/ai/rag/claude.py`** — now imports `get_mode_config` from `llm_config.py` (no more hardcoded dict)
- **`app/ai/ingestion/pinecone.py`** — added `delete_vectors(ids, namespace)` for document cleanup
- **`app/main.py`** — includes the admin router

## Analytics endpoints

All endpoints: `GET /admin/analytics/...` — guarded by `admin_guard` (role `admin` or `super_admin`).

| Endpoint | Query params | Data sources |
|----------|-------------|--------------|
| `/overview` | — | `users`, `platform_sessions`, `sessions`, `conversations`, `session_surveys`, `review_summaries`, `ws_sessions` |
| `/users` | `days=30` | `users`, `platform_sessions` |
| `/sessions` | `days=30` | `sessions`, `mode_sessions`, `ws_sessions` |
| `/feedback` | `days=90` | `session_surveys`, `mode_feedback`, `user_feedback`, `content_quality_flags` |
| `/learning-outcomes` | `days=30` | `review_summaries`, `review_attempts`, `session_surveys` |
| `/performance` | `days=30` | `request_metrics` |

### `/overview` response shape

```json
{
  "total_users": 150,
  "active_users_30d": 45,
  "total_sessions": 320,
  "sessions_completed": 210,
  "total_conversations": 1240,
  "total_reviews_completed": 89,
  "avg_overall_rating": 4.2,
  "avg_review_score": 3.8,
  "total_platform_minutes": 4820.5,
  "survey_count": 120,
  "user_feedback_count": 23,
  "mode_feedback_count": 156
}
```

### `/feedback` response shape

```json
{
  "session_surveys": {
    "count": 120,
    "avg_clarity": 4.1,
    "avg_helpfulness": 4.3,
    "avg_confidence": 3.9,
    "avg_overall": 4.2
  },
  "mode_feedback": {
    "learn":    { "count": 45, "avg_ease": 3.8, "avg_engagement": 4.1, "avg_usefulness": 4.0 },
    "practice": { "count": 60, "avg_ease": 3.5, "avg_engagement": 4.2, "avg_usefulness": 3.9 },
    "review":   { "count": 51, "avg_ease": 3.2, "avg_engagement": 3.8, "avg_usefulness": 4.1 }
  },
  "user_feedback": {
    "total": 23,
    "by_type": { "bug": 3, "suggestion": 8, "content": 5, "ux": 4, "other": 3 },
    "recent": [...]
  },
  "content_quality_flags": [...]
}
```

## User management endpoints

| Method | Endpoint | Access | Notes |
|--------|----------|--------|-------|
| `GET` | `/admin/users` | admin+ | Paginated. Query params: `page`, `page_size`, `role`, `search` |
| `GET` | `/admin/users/{id}` | admin+ | Includes session/review stats and recent feedback |
| `PATCH` | `/admin/users/{id}/role` | admin+ | Cannot assign a role higher than your own |
| `DELETE` | `/admin/users/{id}` | super_admin | Deletes from `auth.users` (cascades to `public.users`) |

## Document management endpoints

| Method | Endpoint | Access | Notes |
|--------|----------|--------|-------|
| `GET` | `/admin/documents` | admin+ | Paginated. Filter by `status`, `course_id`, `doc_type` |
| `GET` | `/admin/documents/{id}` | admin+ | Includes chunk list preview and Pinecone namespaces |
| `PATCH` | `/admin/documents/{id}` | admin+ | Update `title`, `difficulty`, `version`, `doc_type`, `default_mode` |
| `DELETE` | `/admin/documents/{id}` | admin+ | Removes Pinecone vectors, then `document_chunks`, then `documents` |

## Course management endpoints

| Method | Endpoint | Access | Notes |
|--------|----------|--------|-------|
| `GET` | `/admin/courses` | admin+ | Includes document count per course |
| `POST` | `/admin/courses` | admin+ | `id` is user-defined (slug-style) |
| `GET` | `/admin/courses/{id}` | admin+ | Includes full document list |
| `PATCH` | `/admin/courses/{id}` | admin+ | Update name, description, persona, domain_topics |
| `DELETE` | `/admin/courses/{id}` | super_admin | Blocked if documents still exist |

## LLM management endpoints

| Method | Endpoint | Access | Notes |
|--------|----------|--------|-------|
| `GET` | `/admin/llm/config` | admin+ | Returns active config + defaults + available models |
| `PATCH` | `/admin/llm/config/{mode}` | admin+ | Override `model`, `temperature`, `max_tokens` for a mode |
| `POST` | `/admin/llm/config/reset` | super_admin | Deletes `llm_config.json`, restores defaults |
| `GET` | `/admin/llm/usage` | admin+ | Request counts and latency stats grouped by mode |

### Valid modes for LLM config

`learn` · `application` · `review` · `review_generation`

### Available models

- `claude-opus-4-7`
- `claude-opus-4-6`
- `claude-sonnet-4-6`
- `claude-haiku-4-5-20251001`

## Role-based access summary

| Action | `admin` | `super_admin` |
|--------|---------|---------------|
| View all analytics | ✓ | ✓ |
| Manage documents | ✓ | ✓ |
| Manage courses (except delete) | ✓ | ✓ |
| Update LLM config | ✓ | ✓ |
| Update user roles (up to admin) | ✓ | ✓ |
| Delete users | ✗ | ✓ |
| Delete courses | ✗ | ✓ |
| Reset LLM config | ✗ | ✓ |
| Assign super_admin role | ✗ | ✓ |
