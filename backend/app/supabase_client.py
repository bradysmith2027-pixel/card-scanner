"""
supabase_client.py — factory functions for the two kinds of Supabase client.

SECURITY (see the schema design doc's Security Requirements):
  - user_client(jwt): built with the ANON key and the caller's JWT. RLS is
    enforced, so it can only touch the logged-in user's rows. Use this for
    ALL per-user requests.
  - service_client(): built with the SERVICE ROLE key, which BYPASSES RLS.
    God mode. Use ONLY for admin/ops jobs (e.g. pg_dump), never in a path
    driven by user input.
"""

from supabase import Client, create_client

from app.config import get_settings


def user_client(user_jwt: str) -> Client:
    """
    A Supabase client scoped to one user. RLS applies: every query is
    automatically limited to rows where user_id = auth.uid().
    """
    settings = get_settings()
    settings.require("supabase_url", "supabase_anon_key")
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    # Forward the user's access token so PostgREST runs queries as that user.
    client.postgrest.auth(user_jwt)
    return client


def service_client() -> Client:
    """
    Admin client that BYPASSES RLS. Reserve for ops/admin only. Do not build
    this from a request handler that acts on user-supplied identifiers.
    """
    settings = get_settings()
    settings.require("supabase_url", "supabase_service_role_key")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
