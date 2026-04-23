import os
import threading
from supabase import create_client, Client, create_async_client, AsyncClient

from app.config import load_env

_thread_local = threading.local()
_async_supabase = None
_supabase_anon = None


def get_supabase() -> Client:
    """Service role client — bypasses RLS. Only for auth.admin.* calls and Celery tasks."""
    if not hasattr(_thread_local, "client"):
        load_env()
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        _thread_local.client = create_client(url, key)
    return _thread_local.client


def get_supabase_anon() -> Client:
    """Anon-key client for auth API calls (signup, login, OAuth). No user JWT."""
    global _supabase_anon
    if _supabase_anon is None:
        load_env()
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_ANON_KEY")
        if not url or not key:
            raise RuntimeError("Missing SUPABASE_URL or SUPABASE_ANON_KEY")
        _supabase_anon = create_client(url, key)
    return _supabase_anon


def get_user_client(jwt: str) -> Client:
    """Anon-key client with the user's JWT injected. RLS is fully enforced."""
    if not hasattr(_thread_local, "anon_client"):
        load_env()
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_ANON_KEY")
        if not url or not key:
            raise RuntimeError("Missing SUPABASE_URL or SUPABASE_ANON_KEY")
        _thread_local.anon_client = create_client(url, key)
    client = _thread_local.anon_client
    client.postgrest.auth(jwt)
    return client


def reset_async_supabase() -> None:
    # Celery tasks must call this before using get_async_supabase so workers
    # don't reuse the parent process's client across fork boundaries.
    global _async_supabase
    _async_supabase = None


async def get_async_supabase() -> AsyncClient:
    """Singleton async service-role client for Celery tasks."""
    global _async_supabase
    if _async_supabase is None:
        load_env()
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        client = await create_async_client(url, key)
        # Guard against two coroutines racing through the None check above.
        if _async_supabase is None:
            _async_supabase = client
    return _async_supabase
