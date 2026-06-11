"""Shared supabase stub for the chain-event client-build tests.

Both test_chain_event_emit and test_chain_event_shipper need a fake ``supabase``
module to assert the PostgREST timeout is threaded into ``create_client`` —
without installing supabase-py or touching the network. Keeping the stub here
(rather than copy-pasted per suite) stops the two copies from drifting, which
they had already started to (one captured ``create_client`` kwargs, the other
didn't).
"""
from __future__ import annotations

import types
from typing import Any, Optional


class FakeClientOptions:
    """Stand-in for supabase-py's ClientOptions capturing the timeout kwarg."""

    def __init__(self, postgrest_client_timeout=None, **kwargs):
        self.postgrest_client_timeout = postgrest_client_timeout
        self.extra = kwargs


def make_fake_supabase(
    capture: Optional[dict[str, Any]] = None,
    *,
    reject_options: bool = False,
) -> types.ModuleType:
    """Build a fake ``supabase`` module recording ``create_client``'s args.

    Resolves both lookups the client builders make: ``from supabase import
    create_client`` and ``from supabase import ClientOptions``.

    ``capture`` (a dict the caller owns) receives the url/key/options/extra
    kwargs of the LAST create_client call. When ``reject_options`` is set,
    create_client raises TypeError if handed a non-None ``options=`` — emulating
    a supabase-py whose create_client signature predates or renamed the param,
    to exercise build_client's un-pinned fallback.
    """
    capture = capture if capture is not None else {}
    mod = types.ModuleType('supabase')

    def _fake_create_client(url, key, options=None, **kwargs):
        if reject_options and options is not None:
            raise TypeError(
                "create_client() got an unexpected keyword argument 'options'")
        capture['url'] = url
        capture['key'] = key
        capture['options'] = options
        capture['extra_kwargs'] = kwargs
        return object()  # opaque stand-in client; never network-touched

    mod.create_client = _fake_create_client  # type: ignore[attr-defined]
    mod.ClientOptions = FakeClientOptions  # type: ignore[attr-defined]
    return mod
