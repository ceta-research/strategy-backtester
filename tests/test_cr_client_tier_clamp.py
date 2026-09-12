"""Tests for tier-aware resource clamping in lib/cr_client.py."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from lib.cr_client import CetaResearch

FREE_LIMITS = {
    "maxMemory": 3072, "maxThreads": 1, "maxDisk": 9216,
    "maxExecutionTimeout": 120, "maxWaitTimeout": 300,
    "defaultMemory": 3072, "defaultThreads": 1, "defaultDisk": 3072,
    "defaultExecutionTimeout": 60, "defaultWaitTimeout": 300,
}
PREMIUM_LIMITS = {
    "maxMemory": 16384, "maxThreads": 6, "maxDisk": 40960,
    "maxExecutionTimeout": 600, "maxWaitTimeout": 600,
    "defaultMemory": 6144, "defaultThreads": 3, "defaultDisk": 6144,
    "defaultExecutionTimeout": 180, "defaultWaitTimeout": 600,
}
ALL_TIER_LIMITS = {"free": FREE_LIMITS, "premium": PREMIUM_LIMITS}


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Stands in for requests.Session; records calls, serves canned replies."""

    def __init__(self, limits_status=200, limits_body=None):
        self.headers = {}
        self.get_calls = []
        self.post_calls = []
        self._limits_status = limits_status
        self._limits_body = limits_body

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return FakeResponse(self._limits_status, self._limits_body)

    def post(self, url, json=None, **kwargs):
        self.post_calls.append((url, json))
        return FakeResponse(201, {"taskId": "task-0000"})


def make_client(user_tier="free", limits_status=200, limits_body=None):
    client = CetaResearch(api_key="test-key")
    if limits_body is None and limits_status == 200:
        limits_body = {"userTier": user_tier, "allTierLimits": ALL_TIER_LIMITS}
    client.session = FakeSession(limits_status=limits_status, limits_body=limits_body)
    return client


def submitted_body(client):
    assert len(client.session.post_calls) >= 1
    return client.session.post_calls[-1][1]


def test_free_tier_clamps_all_knobs():
    client = make_client("free")
    client._submit("SELECT 1", timeout=600, memory_mb=16384, threads=6, disk_mb=40960)
    resources = submitted_body(client)["resources"]
    assert resources == {
        "memoryMb": 3072,
        "threads": 1,
        "diskMb": 9216,
        "executionTimeoutSeconds": 120,
    }


def test_premium_tier_passes_through_unchanged():
    client = make_client("premium")
    client._submit("SELECT 1", timeout=600, memory_mb=16384, threads=6, disk_mb=40960)
    resources = submitted_body(client)["resources"]
    # min(requested, cap) is a no-op at premium; executionTimeoutSeconds is
    # newly sent (raises the ceiling from tier default to the requested value).
    assert resources == {
        "memoryMb": 16384,
        "threads": 6,
        "diskMb": 40960,
        "executionTimeoutSeconds": 600,
    }


def test_limits_fetch_failure_omits_resources():
    client = make_client(limits_status=404, limits_body={})
    client._submit("SELECT 1", timeout=600, memory_mb=16384, threads=6, disk_mb=40960)
    assert "resources" not in submitted_body(client)


def test_execution_timeout_capped_at_tier_max():
    client = make_client("free")
    client._submit("SELECT 1", timeout=600, memory_mb=1024)
    assert submitted_body(client)["resources"]["executionTimeoutSeconds"] == 120

    client = make_client("free")
    client._submit("SELECT 1", timeout=60, memory_mb=1024)
    assert submitted_body(client)["resources"]["executionTimeoutSeconds"] == 60


def test_disk_only_sent_when_caller_passed_it():
    client = make_client("free")
    client._submit("SELECT 1", timeout=300, memory_mb=16384, threads=6)
    resources = submitted_body(client)["resources"]
    assert "diskMb" not in resources
    assert resources["memoryMb"] == 3072


def test_anonymous_tier_treated_as_fetch_failure():
    # /resource-limits answers unauthenticated calls with userTier
    # "anonymous" (15s execution cap). An authenticated client seeing that
    # means its auth didn't take; fall back to omitting resources rather
    # than clamping every query to anonymous caps.
    body = {
        "userTier": "anonymous",
        "allTierLimits": {"anonymous": {"maxMemory": 1024, "maxThreads": 1,
                                        "maxDisk": 1024, "maxExecutionTimeout": 15,
                                        "maxWaitTimeout": 60}},
    }
    client = make_client(limits_body=body)
    client._submit("SELECT 1", timeout=300, memory_mb=16384, threads=6)
    assert "resources" not in submitted_body(client)


def test_limits_fetch_uses_client_session_and_endpoint():
    # The limits GET must ride the same session as _submit -- that session
    # carries the X-API-Key header (asserted on the real session below).
    client = CetaResearch(api_key="test-key")
    assert client.session.headers["X-API-Key"] == "test-key"
    fake = FakeSession(limits_body={"userTier": "free", "allTierLimits": ALL_TIER_LIMITS})
    client.session = fake
    client._submit("SELECT 1", timeout=300, memory_mb=16384)
    assert len(fake.get_calls) == 1
    url, kwargs = fake.get_calls[0]
    assert url == f"{client.base_url}/resource-limits"
    assert kwargs.get("timeout") == 10


def test_limits_fetched_once_per_process():
    client = make_client("free")
    client._submit("SELECT 1", timeout=300, memory_mb=16384)
    client._submit("SELECT 2", timeout=300, memory_mb=16384)
    assert len(client.session.get_calls) == 1
    assert len(client.session.post_calls) == 2


def test_incomplete_limits_dict_treated_as_fetch_failure():
    # A tier dict missing keys the clamp reads must fall back to
    # omit-resources, not raise KeyError out of _submit.
    body = {"userTier": "free", "allTierLimits": {"free": {"maxMemory": 3072}}}
    client = make_client(limits_body=body)
    client._submit("SELECT 1", timeout=300, memory_mb=16384, threads=6)
    assert "resources" not in submitted_body(client)


def test_fetch_failure_cached_no_refetch():
    client = make_client(limits_status=404, limits_body={})
    client._submit("SELECT 1", timeout=300, memory_mb=16384)
    client._submit("SELECT 2", timeout=300, memory_mb=16384)
    assert len(client.session.get_calls) == 1
