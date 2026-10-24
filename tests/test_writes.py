"""Writes: post(), put() and create_payment(), against a fake `requests`.

Nothing here reaches Xero: `requests.request` is replaced for every test, and the
credentials and token store are throwaway files in a temporary config directory.
"""

import datetime
import json
import time

import pytest

import xero_api.client as client_mod
from xero_api import XeroClient, XeroError


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class Calls(list):
    """The requests made, plus `queue`: the responses to give, in order."""

    def __init__(self):
        super().__init__()
        self.queue = []


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


@pytest.fixture
def calls(monkeypatch):
    """Every request made, answered from a queue of responses (default: 200 with {})."""
    made = Calls()

    def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
        made.append({"method": method, "url": url, "headers": headers, "json": json})
        return made.queue.pop(0) if made.queue else FakeResponse(200, {})

    monkeypatch.setattr(client_mod.requests, "request", fake_request)
    return made


def test_post_and_put_send_json_with_the_tenant(client, calls):
    client.post("Invoices", {"Invoices": []})
    client.put("Payments", {"Payments": []})
    assert [c["method"] for c in calls] == ["POST", "PUT"]
    assert calls[0]["url"] == "https://api.xero.com/api.xro/2.0/Invoices"
    assert calls[0]["headers"]["Authorization"] == "Bearer at-1"
    assert calls[0]["headers"]["Xero-tenant-id"] == "tenant-1"
    assert "Idempotency-Key" not in calls[0]["headers"]


def test_idempotency_key_is_sent_as_a_header(client, calls):
    client.post("Invoices", {}, idempotency_key="key-1")
    client.put("Payments", {}, idempotency_key="key-2")
    assert [c["headers"]["Idempotency-Key"] for c in calls] == ["key-1", "key-2"]
    with pytest.raises(XeroError, match="128"):
        client.put("Payments", {}, idempotency_key="k" * 129)


def test_a_failed_write_raises_with_xeros_answer(client, calls):
    calls.queue.append(FakeResponse(400, {"Message": "A validation exception occurred"}))
    with pytest.raises(XeroError, match=r"PUT .*Payments failed \(400\).*validation"):
        client.put("Payments", {})


def test_a_401_refreshes_once_then_retries(client, calls, monkeypatch):
    calls.queue.append(FakeResponse(401, {}))
    monkeypatch.setattr(client, "refresh", lambda: "at-2")
    client.put("Payments", {})
    assert [c["headers"]["Authorization"] for c in calls] == ["Bearer at-1", "Bearer at-2"]


def test_create_payment_by_number_and_code(client, calls):
    calls.queue.append(FakeResponse(200, {"Payments": [{"PaymentID": "p-1", "Amount": 1500.0}]}))
    out = client.create_payment("INV-0001", "090", 1500.0, datetime.date(2026, 11, 2),
                                reference="INV-0001 DEPOSIT", idempotency_key="pay-1")
    assert out == {"PaymentID": "p-1", "Amount": 1500.0}
    (call,) = calls
    assert call["method"] == "PUT" and call["url"].endswith("/Payments")
    assert call["headers"]["Idempotency-Key"] == "pay-1"
    assert call["json"] == {"Payments": [{
        "Invoice": {"InvoiceNumber": "INV-0001"}, "Account": {"Code": "090"},
        "Date": "2026-11-02", "Amount": 1500.0, "Reference": "INV-0001 DEPOSIT"}]}


def test_create_payment_by_ids_without_a_reference(client, calls):
    calls.queue.append(FakeResponse(200, {"Payments": [{"PaymentID": "p-2"}]}))
    client.create_payment({"InvoiceID": "i-1"}, {"AccountID": "a-1"}, 10, "2026-11-02")
    body = calls[0]["json"]["Payments"][0]
    assert body == {"Invoice": {"InvoiceID": "i-1"}, "Account": {"AccountID": "a-1"},
                    "Date": "2026-11-02", "Amount": 10}


def test_create_payment_refuses_a_bad_amount_without_calling(client, calls):
    for bad in (0, -5, "100", None, True):
        with pytest.raises(XeroError, match="positive number"):
            client.create_payment("INV-0001", "090", bad, "2026-11-02")
    assert calls == []


def test_create_payment_reports_validation_errors(client, calls):
    calls.queue.append(FakeResponse(200, {"Payments": [{"ValidationErrors": [
        {"Message": "Payment amount exceeds the amount outstanding on this document"}]}]}))
    with pytest.raises(XeroError, match="exceeds the amount outstanding"):
        client.create_payment("INV-0001", "090", 99999, "2026-11-02")
    calls.queue.append(FakeResponse(200, {"Payments": []}))
    with pytest.raises(XeroError, match="no payment"):
        client.create_payment("INV-0001", "090", 1, "2026-11-02")
