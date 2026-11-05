"""Several clients (standing in for several processes) sharing one token store.

Xero rotates the refresh token on every refresh and refuses a consumed one with
invalid_grant. The fake token endpoint below does the same, so a client that
refreshes with a stale in-memory token fails here exactly as it would against Xero.
Nothing reaches Xero.
"""

import json
import threading
import time

import pytest

import xero_api.client as client_mod
from xero_api import XeroClient, XeroError, update_store


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.content = self.text.encode()
        self.headers = {}

    def json(self):
        return self._body


class FakeXero:
    """A token endpoint that rotates refresh tokens and rejects consumed ones."""

    def __init__(self, live_rt):
        self.live = {live_rt}
        self.n = 1
        self.posts = []
        self.lock = threading.Lock()

    def post(self, url, headers=None, data=None, timeout=None, **kw):
        with self.lock:
            rt = data["refresh_token"]
            self.posts.append(rt)
            if rt not in self.live:
                return FakeResponse(400, {"error": "invalid_grant"})
            self.live.discard(rt)
            self.n += 1
            new_rt = f"rt-{self.n}"
            self.live.add(new_rt)
        time.sleep(0.01)  # widen the window a racing client would slip into
        return FakeResponse(200, {"access_token": f"at-{self.n}", "refresh_token": new_rt,
                                  "expires_in": 1800, "scope": "s"})

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        return FakeResponse(200, {"auth": headers["Authorization"]})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XERO_HOME", str(tmp_path))
    monkeypatch.delenv("XERO_CLIENT_ID", raising=False)
    monkeypatch.delenv("XERO_CLIENT_SECRET", raising=False)
    (tmp_path / ".env").write_text("XERO_CLIENT_ID=test-id\nXERO_CLIENT_SECRET=test-secret\n")
    (tmp_path / "tokens.json").write_text(json.dumps({
        "version": 2, "active": "demo",
        "profiles": {
            "demo": {"access_token": "at-1", "refresh_token": "rt-1",
                     "expires_at": time.time() + 3600, "tenant_id": "tenant-1",
                     "tenant_name": "Demo",
                     "connections": [{"tenantId": "tenant-1", "tenantName": "Demo"}]},
            "real": {"access_token": "real-at", "refresh_token": "real-rt",
                     "expires_at": 123, "tenant_id": "tenant-r", "tenant_name": "Real"},
        },
    }))
    return tmp_path


@pytest.fixture
def xero(monkeypatch):
    fake = FakeXero("rt-1")
    monkeypatch.setattr(client_mod.requests, "post", fake.post)
    monkeypatch.setattr(client_mod.requests, "get", fake.get)
    return fake


def disk(home):
    return json.loads((home / "tokens.json").read_text())


def expire(client):
    client.prof["expires_at"] = 0


def test_a_stale_client_adopts_the_token_another_client_refreshed(home, xero):
    a, b = XeroClient(), XeroClient()
    b.refresh()
    assert disk(home)["profiles"]["demo"]["refresh_token"] == "rt-2"

    # A still holds rt-1, which Xero has consumed. It must not send it.
    assert a.refresh() == "at-2"
    assert xero.posts == ["rt-1"]
    assert a.prof["refresh_token"] == "rt-2"


def test_a_stale_client_with_an_expired_token_reads_disk_before_refreshing(home, xero):
    a, b = XeroClient(), XeroClient()
    b.refresh()
    expire(a)
    assert a.get("Organisation") == {"auth": "Bearer at-2"}
    assert xero.posts == ["rt-1"]


def test_when_the_disk_access_token_has_expired_the_disk_refresh_token_is_used(home, xero):
    a, b = XeroClient(), XeroClient()
    b.refresh()
    update_store(lambda s: s["profiles"]["demo"].update(expires_at=0))
    expire(a)
    assert a.ensure_token() == "at-3"
    assert xero.posts == ["rt-1", "rt-2"]  # never the consumed rt-1 twice
    assert disk(home)["profiles"]["demo"]["refresh_token"] == "rt-3"


def test_a_stale_save_never_overwrites_a_newer_token_or_other_profiles(home, xero):
    a, b = XeroClient(), XeroClient()
    b.refresh()
    # Meanwhile someone re-authorizes "real" and switches the active profile.
    update_store(lambda s: (s["profiles"]["real"].update(refresh_token="real-rt-new"),
                            s.update(active="real")))

    a.switch_tenant("tenant-1")  # A saves from its stale in-memory store

    after = disk(home)
    assert after["profiles"]["demo"]["refresh_token"] == "rt-2"
    assert after["profiles"]["demo"]["access_token"] == "at-2"
    assert after["profiles"]["real"]["refresh_token"] == "real-rt-new"
    assert after["active"] == "real"


def test_a_refresh_leaves_other_profiles_and_tenant_switches_alone(home, xero):
    a = XeroClient()
    update_store(lambda s: (s["profiles"]["real"].update(refresh_token="real-rt-new"),
                            s["profiles"]["demo"].update(tenant_id="tenant-2")))
    expire(a)
    a.refresh()
    after = disk(home)
    assert after["profiles"]["real"]["refresh_token"] == "real-rt-new"
    assert after["profiles"]["demo"]["tenant_id"] == "tenant-2"
    assert after["profiles"]["demo"]["refresh_token"] == "rt-2"


def test_racing_clients_refresh_once_and_all_succeed(home, xero):
    clients = [XeroClient() for _ in range(6)]
    for c in clients:
        expire(c)
    update_store(lambda s: s["profiles"]["demo"].update(expires_at=0))
    errors, tokens = [], []

    def run(c):
        try:
            tokens.append(c.ensure_token())
        except XeroError as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert xero.posts == ["rt-1"]
    assert set(tokens) == {"at-2"}


def test_a_failed_refresh_leaves_the_store_untouched(home, xero):
    a = XeroClient()
    xero.live.clear()
    before = (home / "tokens.json").read_text()
    with pytest.raises(XeroError, match="invalid_grant"):
        a.refresh()
    assert (home / "tokens.json").read_text() == before


def test_the_lock_file_is_private(home, xero):
    XeroClient().refresh()
    assert ((home / "tokens.json.lock").stat().st_mode & 0o777) == 0o600
