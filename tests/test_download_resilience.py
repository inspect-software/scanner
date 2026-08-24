"""Download figures must not depend on the scanner's own request rate.

The locust incident, distilled: pypistats throttled a bulk-scan wave, the code
filed 429 under "no data", and the report carried a same-name stranger's 25
downloads/month while the real figure was 14M. Three guarantees pin the fix:

- a 429 is retried, honoring Retry-After;
- a figure that could not be fetched is recorded as *failed*, distinct from a
  registry that publishes nothing, and the report says so in a warning;
- a failed fetch keeps the previous scan's figures (marked carried_forward)
  rather than opening a hole the adoption metric falls into.
"""

from __future__ import annotations

import httpx
import pytest

import scanner.ecosystems as eco
from scanner.collect import _carry_forward_downloads
from scanner.ecosystems import _get, _get_stat, fetch_pypi
from scanner.models import EcosystemPackage


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    naps: list[float] = []
    monkeypatch.setattr(eco.time, "sleep", lambda s: naps.append(s))
    return naps


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- 429 handling in _get ---------------------------------------------------


def test_429_is_retried_and_recovers(instant_sleep):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"data": {"last_month": 14_264_040}})

    with _client(handler) as client:
        resp = _get(client, "https://pypistats.org/api/packages/locust/recent")

    assert resp.status_code == 200
    assert instant_sleep == [7.0]  # Retry-After honored, not the 1s default


def test_retry_after_is_capped(instant_sleep):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with _client(handler) as client:
        resp = _get(client, "https://x/stats")

    assert resp.status_code == 429  # still throttled after every attempt
    assert all(nap == eco._RATE_LIMIT_MAX_WAIT_SECONDS for nap in instant_sleep)
    assert len(instant_sleep) == eco._FETCH_ATTEMPTS - 1


def test_rate_limit_waiting_is_budgeted_per_client(instant_sleep):
    """One throttled service must not compound into a scan that sleeps for
    minutes: once the client's budget is spent, 429s return immediately and
    flow into the failed/carry-forward path."""
    def handler(request):
        return httpx.Response(429)

    with _client(handler) as client:
        exhausted_after = int(eco._RATE_LIMIT_CLIENT_BUDGET_SECONDS / eco._RATE_LIMIT_RETRY_DELAY_SECONDS)
        for _ in range(exhausted_after + 5):
            resp = _get(client, "https://x/stats")
            assert resp.status_code == 429

    assert sum(instant_sleep) <= eco._RATE_LIMIT_CLIENT_BUDGET_SECONDS


def test_non_numeric_retry_after_falls_back_to_default(instant_sleep):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "Wed, 19 Aug 2026 18:00:00 GMT"})

    with _client(handler) as client:
        _get(client, "https://x/stats")

    assert all(nap == eco._RATE_LIMIT_RETRY_DELAY_SECONDS for nap in instant_sleep)


# --- state classification ----------------------------------------------------


def test_stat_states():
    responses = {
        "/published": httpx.Response(200, json={"data": {"last_month": 5}}),
        "/unpublished": httpx.Response(404),
        "/limited": httpx.Response(429),
        "/broken": httpx.Response(200, content=b"not json"),
    }

    def handler(request):
        return responses[request.url.path]

    with _client(handler) as client:
        assert _get_stat(client, "https://x/published", "data", "last_month") == (5, "published")
        assert _get_stat(client, "https://x/unpublished", "data", "last_month") == (None, "unpublished")
        assert _get_stat(client, "https://x/limited", "data", "last_month") == (None, "failed")
        assert _get_stat(client, "https://x/broken", "data", "last_month") == (None, "failed")


def test_pypi_package_marked_failed_when_stats_stay_limited():
    def handler(request):
        if request.url.host == "pypi.org":
            return httpx.Response(200, json={
                "info": {"name": "locust", "version": "2.0.0",
                         "project_urls": {"Source": "https://github.com/locustio/locust"}},
                "releases": {},
            })
        return httpx.Response(429)

    with _client(handler) as client:
        pkg = fetch_pypi(client, "locust", "locustio/locust")

    assert pkg.monthly_downloads is None
    assert pkg.downloads_state == "failed"


# --- carry-forward -----------------------------------------------------------


def _pkg(**kw) -> EcosystemPackage:
    base = dict(ecosystem="pypi", name="locust", registry_url="x", matches_repo=True)
    base.update(kw)
    return EcosystemPackage(**base)


def test_failed_fetch_carries_previous_figures_forward():
    current = [_pkg(downloads_state="failed")]
    prior = [_pkg(monthly_downloads=14_264_040, total_downloads=900_000_000,
                  downloads_state="published")]
    warnings: list[str] = []

    _carry_forward_downloads(current, prior, warnings)

    assert current[0].monthly_downloads == 14_264_040
    assert current[0].downloads_state == "carried_forward"
    assert any("carried forward" in w for w in warnings)


def test_unpublished_is_a_fact_and_is_not_patched():
    current = [_pkg(downloads_state="unpublished")]
    prior = [_pkg(monthly_downloads=1_000)]
    _carry_forward_downloads(current, prior, [])
    assert current[0].monthly_downloads is None
    assert current[0].downloads_state == "unpublished"


def test_unverified_packages_never_receive_carried_figures():
    """Carrying a figure onto a package the registry no longer ties back here
    would re-create the original defect with our own stored data."""
    current = [_pkg(downloads_state="failed", matches_repo=None)]
    prior = [_pkg(monthly_downloads=1_000)]
    _carry_forward_downloads(current, prior, [])
    assert current[0].monthly_downloads is None
    assert current[0].downloads_state == "failed"


def test_figures_from_an_unverified_prior_package_are_not_carried():
    current = [_pkg(downloads_state="failed")]
    prior = [_pkg(monthly_downloads=1_000, matches_repo=None)]
    _carry_forward_downloads(current, prior, [])
    assert current[0].monthly_downloads is None


def test_carry_forward_chains_across_consecutive_failures():
    current = [_pkg(downloads_state="failed")]
    prior = [_pkg(monthly_downloads=5_000, downloads_state="carried_forward")]
    _carry_forward_downloads(current, prior, [])
    assert current[0].monthly_downloads == 5_000
    assert current[0].downloads_state == "carried_forward"


def test_no_prior_data_leaves_the_failure_visible():
    current = [_pkg(downloads_state="failed")]
    _carry_forward_downloads(current, None, [])
    assert current[0].downloads_state == "failed"


def test_an_oversized_manifest_is_refused_rather_than_buffered():
    """A repository chooses the size of its own package.json.

    Every other fetch in `ecosystems` addresses a registry; this one reads
    raw.githubusercontent, where GitHub serves single files up to 100 MB. A scan
    reads up to twenty manifests and keeps every text for classification, so a
    padded manifest was a way to take a worker's memory by committing a file.
    """
    import httpx
    from scanner import ecosystems as eco

    pulled = {"bytes": 0}

    class _Padded(httpx.SyncByteStream):
        def __iter__(self):
            chunk = bytes(65536)
            while True:
                pulled["bytes"] += len(chunk)
                yield chunk

    def handle(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Padded())

    client = httpx.Client(transport=httpx.MockTransport(handle))
    text = eco._get_manifest_text(client, "https://raw.githubusercontent.com/a/b/main/package.json")

    assert text is None
    # Stopped at the cap, not somewhere past it.
    assert pulled["bytes"] <= eco._MANIFEST_MAX_BYTES + 65536


def test_a_normal_manifest_still_reads_through():
    import httpx
    from scanner import ecosystems as eco

    body = b'{"name": "widget"}'
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    )
    assert eco._get_manifest_text(client, "https://raw.githubusercontent.com/a/b/main/package.json") == '{"name": "widget"}'
