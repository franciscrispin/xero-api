"""Reads: get() and download(), against a fake `requests`.

Nothing here reaches Xero: `requests.get` is replaced for every test, and the
credentials and token store are throwaway files in a temporary config directory.

The case worth protecting is download(): an attachment is bytes, and a PDF that
goes out through `resp.text` comes back unopenable.
"""

import json
import time

import pytest

import xero_api.client as client_mod
from xero_api import XeroClient, XeroError


class FakeResponse:
    def __init__(self, status, content, headers=None, body=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._body = body
        self.text = content.decode("utf-8", "replace")

    def json(self):
        return self._body if self._body is not None else json.loads(self.text)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XERO_HOME", str(tmp_path))
    monkeypatch.delenv("XERO_CLIENT_ID", raising=False)
    monkeypatch.delenv("XERO_CLIENT_SECRET", raising=False)
    (tmp_path / ".env").write_text("XERO_CLIENT_ID=test-id\nXERO_CLIENT_SECRET=test-secret\n")
    (tmp_path / "tokens.json").write_text(json.dumps({
        "version": 2, "active": "demo",
        "profiles": {"demo": {"access_token": "at-1", "refresh_token": "rt-1",
                              "expires_at": time.time() + 3600, "tenant_id": "tenant-1",
                              "tenant_name": "Demo Company (Global)"}},
    }))
    return XeroClient()


class Calls(list):
    """The requests made, plus `queue`: the responses to give, in order."""

    def __init__(self):
        super().__init__()
        self.queue = []


@pytest.fixture
def gets(monkeypatch):
    """Every GET made, answered from a queue of responses (default: 200 with {})."""
    made = Calls()

    def fake_get(url, headers=None, params=None, timeout=None, **kw):
        made.append({"url": url, "headers": headers, "params": params})
        return made.queue.pop(0) if made.queue else FakeResponse(200, b"{}")

    monkeypatch.setattr(client_mod.requests, "get", fake_get)
    return made


# A PDF header plus a byte that is not valid UTF-8, which is what makes the
# difference between returning bytes and returning text visible.
PDF = b"%PDF-1.4\n\xff\xfe binary \x00 body"


def test_download_returns_the_body_unchanged(client, gets):
    gets.queue.append(FakeResponse(200, PDF))
    data = client.download("Invoices/abc/Attachments/Summary.pdf",
                           accept="application/pdf")
    assert data == PDF
    assert gets[0]["headers"]["Accept"] == "application/pdf"
    assert gets[0]["headers"]["Xero-tenant-id"] == "tenant-1"


def test_get_still_parses_json(client, gets):
    gets.queue.append(FakeResponse(200, b'{"Invoices": [{"InvoiceNumber": "INV463"}]}'))
    assert client.get("Invoices")["Invoices"][0]["InvoiceNumber"] == "INV463"
    assert gets[0]["headers"]["Accept"] == "application/json"


def test_get_with_a_text_accept_returns_text(client, gets):
    gets.queue.append(FakeResponse(200, b"a,b\n1,2\n"))
    assert client.get("Reports/X", accept="text/csv") == "a,b\n1,2\n"


def test_a_401_refreshes_once_then_retries(client, gets, monkeypatch):
    monkeypatch.setattr(client, "refresh", lambda: "at-2")
    gets.queue.extend([FakeResponse(401, b"expired"), FakeResponse(200, PDF)])
    assert client.download("Invoices/abc/Attachments/Summary.pdf") == PDF
    assert gets[0]["headers"]["Authorization"] == "Bearer at-1"
    assert gets[1]["headers"]["Authorization"] == "Bearer at-2"


def test_an_error_raises_rather_than_writing_a_broken_file(client, gets):
    gets.queue.append(FakeResponse(404, b"No attachment by that name"))
    with pytest.raises(XeroError) as e:
        client.download("Invoices/abc/Attachments/Nope.pdf")
    assert "404" in str(e.value)
