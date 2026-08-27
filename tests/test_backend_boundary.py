"""Names the inspect.software backend reaches for are stable, public API.

The scanner is consumed by a backend that lives in another repository (the
inspect-software workspace). Three names cross that boundary, and nothing in
this package uses two of them — so without this test, every one of them could
be renamed with the whole suite staying green, and the consumer would break on
its next deploy:

* ``github.rate_limit_observer`` — a module-level slot the backend's token
  pool assigns to persist rate-limit events for its operator dashboard
  (``app/github_pool.py``). It is a deliberate hook.
* ``github._token_cooldowns`` — the per-process exhausted-token map. The
  backend writes reset times into it directly to pre-warm a fresh worker from
  its database. The leading underscore is historical: the moment an external
  consumer depended on it, it stopped being private. It keeps the name because
  renaming it is exactly the break this test exists to prevent.
* ``ecosystems.FETCHERS`` — the registry-name → fetcher map the backend's
  dependency backfill iterates to refresh package metadata outside a scan.

Removing or reshaping any of these is a **breaking change**: it needs a major
version bump and a coordinated change in the workspace, not a refactor in
passing. ``token_fingerprint`` is asserted alongside them because the observer
protocol hands fingerprints to the backend, which stores and compares them —
the two are one contract.
"""

from __future__ import annotations

from scanner import ecosystems, github


def test_rate_limit_observer_slot_exists_and_defaults_to_none() -> None:
    assert hasattr(github, "rate_limit_observer")
    assert github.rate_limit_observer is None or callable(github.rate_limit_observer)


def test_rate_limit_observer_is_invoked_with_the_documented_signature() -> None:
    """The backend's observer stores five positional values; a reordering or an
    added parameter would raise inside its recorder, where exceptions are
    swallowed — the dashboard would just silently go dark."""
    calls: list[tuple] = []
    previous = github.rate_limit_observer
    github.rate_limit_observer = lambda *args: calls.append(args)
    try:
        github._notify_rate_limit("tok", 2, 3, 1234.5, 403)
    finally:
        github.rate_limit_observer = previous

    assert calls == [(github.token_fingerprint("tok"), 2, 3, 1234.5, 403)]


def test_token_cooldowns_is_a_writable_token_to_epoch_map() -> None:
    assert isinstance(github._token_cooldowns, dict)
    github._token_cooldowns["boundary-test-token"] = 1e12
    try:
        assert github._token_cooldowns["boundary-test-token"] == 1e12
    finally:
        del github._token_cooldowns["boundary-test-token"]


def test_token_fingerprint_is_stable() -> None:
    """Stored by the backend across releases; a changed derivation would sever
    every dashboard row from its token."""
    assert github.token_fingerprint("tok") == "1a7674eb4ee78df7"


def test_fetchers_maps_every_supported_registry_to_a_fetcher() -> None:
    assert isinstance(ecosystems.FETCHERS, dict)
    # The backfill iterates this map wholesale, so the contract is its shape,
    # not one favoured entry: string keys, callables taking (client, name,
    # repo_full_name, contacts).
    assert ecosystems.FETCHERS, "FETCHERS must not be empty"
    for key, fetcher in ecosystems.FETCHERS.items():
        assert isinstance(key, str) and callable(fetcher)
    # Registries the backfill knows today. Additions are fine; a removal or
    # rename must be treated as breaking.
    assert {"pypi", "npm", "packagist", "crates", "rubygems", "hex", "go",
            "maven", "nuget"} <= set(ecosystems.FETCHERS)
