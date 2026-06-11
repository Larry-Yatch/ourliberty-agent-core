"""supabase_factory.py — the ONE guarded entrypoint for building a Supabase
client (test-jail Layer B, closes H7).

Every production module that needs a live Supabase client builds it HERE rather
than calling ``supabase.create_client`` directly. The factory calls
``refuse_under_test('supabase')`` first, so an un-mocked client build from a test
process fails LOUD (``TestIsolationBreach``) instead of silently connecting to
the live project with the real service-role key. In production the run sentinel
is never set, so this is a pure pass-through.

The chokepoint census gate (scripts/tests/test_chokepoint_census.py) enforces
that ``create_client(`` appears in NO other scripts/ module — a new direct site
fails the suite at build time.
"""
from test_isolation_guard import refuse_under_test


def get_supabase_client(url, key, options=None):
    """Build a Supabase client through the guarded chokepoint.

    Raises ``TestIsolationBreach`` if reached from a test process. ``options``
    (a supabase ``ClientOptions``) is forwarded when provided so callers that pin
    the PostgREST timeout keep that behavior."""
    refuse_under_test('supabase')
    from supabase import create_client  # type: ignore
    if options is None:
        return create_client(url, key)
    return create_client(url, key, options=options)
