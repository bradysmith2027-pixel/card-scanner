"""
supabase_client.py

Makes the two kinds of Supabase clients.

  - user_client(jwt): uses the anon key plus the user's token. RLS is on, so
    it can only see that user's rows. Use this for every normal request.
  - service_client(): uses the service role key, which skips RLS and can see
    everything. Only for admin stuff like backups, never for anything a user
    can trigger.
"""

from supabase import Client, create_client

from app.config import get_settings


def user_client(user_jwt: str) -> Client:
    """
    A Supabase client for one user. RLS limits every query to rows where
    user_id = auth.uid().
    """
    settings = get_settings()
    settings.require("supabase_url", "supabase_anon_key")
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    # Send the user's token along so the queries run as that user.
    client.postgrest.auth(user_jwt)
    return client


def service_client() -> Client:
    """
    Admin client that skips RLS. Only use it for admin jobs, never inside a
    request handler.
    """
    settings = get_settings()
    settings.require("supabase_url", "supabase_service_role_key")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
