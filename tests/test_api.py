import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app, requests
from app.store import Store, StoreUnavailable, get_store

AUTH = {"Authorization": "Bearer " + "test-only-key-" * 4}
LEAD = {"source_url": "https://example.org/public/post/one", "source_platform": "public_social",
    "observation": "Unverified public allegation", "provenance": {"permitted_public": True, "safety_reviewed": True}}


class FakeStore:
    def __init__(self):
        self.calls = []

    async def rpc(self, name, payload):
        self.calls.append((name, payload))
        if name == "signal_ready_v1":
            return {"schema_version": 1, "rls_tables": 12, "end_user_policies": 0}
        return {"created": True, "lead": {"id": 1, "status": "unverified"}}

    async def rows(self, table, **kwargs):
        self.calls.append((table, kwargs))
        return []

    async def request(self, method, path, **kwargs):
        self.calls.append((path, kwargs))
        return []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SIGNAL_DESK_API_KEY", "test-only-key-" * 4)
    requests.clear()
    fake = FakeStore()
    app.dependency_overrides[get_store] = lambda: fake
    with TestClient(app) as client:
        client.fake = fake
        yield client
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/ready", "/v1/leads", "/v1/assessments", "/v1/collection-runs", "/v1/audit-events", "/openapi.json"])
def test_no_auth_never_touches_database(client, path):
    assert client.get(path).status_code == 401
    assert not client.fake.calls


def test_bad_auth_and_public_health(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/ready", headers=AUTH).status_code == 200


def test_absent_key_is_fail_closed(client, monkeypatch):
    monkeypatch.delenv("SIGNAL_DESK_API_KEY")
    assert client.get("/v1/leads", headers=AUTH).status_code == 503
    assert not client.fake.calls


def test_server_can_store_token_digest_only(client, monkeypatch):
    import hashlib
    token = "test-only-key-" * 4
    digest = hashlib.sha256(token.encode()).hexdigest()
    monkeypatch.delenv("SIGNAL_DESK_API_KEY")
    monkeypatch.setenv("SIGNAL_DESK_API_KEY_SHA256", digest)
    assert client.get("/ready", headers=AUTH).status_code == 200
    assert client.get("/ready", headers={"Authorization": "Bearer " + digest}).status_code == 401


def test_raw_unverified_lead_needs_no_verification(client):
    r = client.post("/v1/leads", headers=AUTH, json=LEAD)
    assert r.status_code == 200
    assert r.json()["lead"]["status"] == "unverified"
    assert client.fake.calls[0][1]["payload"]["provenance"]["media_access"] == "not_inspected"


def test_permalink_identity_preserves_queries_and_ignores_fragment(client):
    for url in ("https://EXAMPLE.org/public/post/one#fragment", LEAD["source_url"]):
        assert client.post("/v1/leads", headers=AUTH, json={**LEAD, "source_url": url}).status_code == 200
    assert client.fake.calls[0][1]["p_key"] == client.fake.calls[1][1]["p_key"]
    client.post("/v1/leads", headers=AUTH, json={**LEAD, "source_url": LEAD["source_url"] + "?post=2"})
    assert client.fake.calls[2][1]["p_key"] != client.fake.calls[1][1]["p_key"]


@pytest.mark.parametrize("change", [
    {"source_url": "http://example.org/post"}, {"source_url": "https://127.0.0.1/post"},
    {"source_url": "https://user:secret@example.org/post"}, {"source_url": "https://example.org:8443/post"},
    {"original_posted_at": "2026-11-03T12:00:00"}, {"private_account_id": "sensitive-test-canary"},
    {"provenance": {"permitted_public": False, "safety_reviewed": True}},
    {"provenance": {"permitted_public": True, "safety_reviewed": False}},
    {"provenance": {"permitted_public": True, "safety_reviewed": True, "media_access": "inspected"}},
])
def test_invalid_input_rejected_without_echo(client, change):
    r = client.post("/v1/leads", headers=AUTH, json={**LEAD, **change})
    assert r.status_code == 422
    assert "sensitive-test-canary" not in r.text
    assert not client.fake.calls


def test_size_and_pagination_bounds(client):
    assert client.post("/v1/leads", headers=AUTH, content="x" * 65537).status_code == 413
    assert client.get("/v1/leads?limit=101", headers=AUTH).status_code == 422
    assert client.get("/v1/leads?before=5&limit=2", headers=AUTH).status_code == 200
    assert client.fake.calls[-1][1]["before"] == 5


def test_security_headers_and_no_public_docs(client):
    r = client.get("/health")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/docs").status_code == 404


def test_assessment_requires_alternatives_and_falsification(client):
    assert client.post("/v1/assessments", headers=AUTH, json={"lead_id": 1, "scenario": "test", "rationale": "test"}).status_code == 422
    payload = {"lead_id": 1, "scenario": "test", "rationale": "test", "alternative_explanations": "Routine event",
        "falsification_criteria": "Contradicting official release", "analysis_threshold": "Analyst-selected review"}
    assert client.post("/v1/assessments", headers=AUTH, json=payload).status_code == 200


def test_database_failure_redacted(client):
    class BrokenStore:
        async def rpc(self, *args):
            raise StoreUnavailable("secret-must-not-appear")
    app.dependency_overrides[get_store] = BrokenStore
    r = client.get("/ready", headers=AUTH)
    assert r.status_code == 503
    assert "secret-must-not-appear" not in r.text


@pytest.mark.parametrize("key,has_bearer", [("sb_secret_test", False), ("legacy-jwt-test", True)])
def test_key_headers_and_no_redirects(monkeypatch, key, has_bearer):
    monkeypatch.setenv("SUPABASE_URL", "https://uzqpndpuqubfygjwnhiv.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", key)
    original = httpx.AsyncClient
    async def handle(request):
        assert ("authorization" in request.headers) is has_bearer
        assert request.headers["apikey"] == key
        return httpx.Response(302, headers={"Location": "https://example.org/steal"})
    def client_factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    with pytest.raises(StoreUnavailable):
        asyncio.run(Store().rows("leads"))


def test_rate_limit(client):
    for _ in range(120):
        assert client.get("/v1/leads", headers=AUTH).status_code == 200
    assert client.get("/v1/leads", headers=AUTH).status_code == 429


def test_reader_key_cannot_write_or_collect(client, monkeypatch):
    import hashlib
    reader = "test-reader-only-token-" * 3
    monkeypatch.setenv("SIGNAL_DESK_READ_KEY_SHA256", hashlib.sha256(reader.encode()).hexdigest())
    headers = {"Authorization": "Bearer " + reader}
    assert client.get("/v1/desk/snapshot", headers=headers).status_code == 200
    client.fake.calls.clear()
    for path, payload in [("/v1/leads", LEAD), ("/v1/collect/federal-reserve", None),
        ("/v1/assessments", {"lead_id":1,"scenario":"test","rationale":"test","alternative_explanations":"routine",
         "falsification_criteria":"opposite","analysis_threshold":"test"}),
        ("/v1/decisions", {"lead_id":1,"decision":"watch","rationale":"test","notification_threshold":"test"})]:
        assert client.post(path, headers=headers, json=payload).status_code == 403
    assert client.fake.calls == []


def test_snapshot_does_not_invent_scenarios(client):
    r = client.get("/v1/desk/snapshot", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["clusters"] == []
    assert r.json()["activity_baselines"] == []
    assert r.json()["coverage"][0]["schedule_verified"] is False


def test_scheduler_credential_has_no_general_api_access(client,monkeypatch):
    import hashlib
    token='test-scheduler-only-'*3
    monkeypatch.setenv('SIGNAL_DESK_SCHEDULER_KEY_SHA256',hashlib.sha256(token.encode()).hexdigest())
    headers={'Authorization':'Bearer '+token}
    assert client.get('/v1/leads',headers=headers).status_code==401
    assert client.post('/v1/leads',headers=headers,json=LEAD).status_code==401
    assert client.post('/v1/pipeline/scheduled',headers={'Authorization':'Bearer wrong'},json={'run_key':'test:lease'}).status_code==401


def test_search_intake_is_not_a_public_or_reader_write_route(client,monkeypatch):
    import hashlib
    body={'batch_id':'test:empty-search','provider':'fixture','searched_at':'2026-11-03T12:00:00Z',
          'queries':[{'id':'test','query':'public infrastructure report'}],'hits':[],'run_limitations':'Fixture only'}
    reader='test-reader-only-token-'*3
    monkeypatch.setenv('SIGNAL_DESK_READ_KEY_SHA256',hashlib.sha256(reader.encode()).hexdigest())
    assert client.post('/v1/search-intake',json=body).status_code==401
    assert client.post('/v1/search-intake',headers={'Authorization':'Bearer '+reader},json=body).status_code==403
    assert client.fake.calls==[]


def test_search_writer_scope_is_limited_to_intake(client,monkeypatch):
    import hashlib
    token='test-intake-only-key-'*3
    monkeypatch.setenv('SIGNAL_DESK_SEARCH_WRITER_KEY_SHA256',hashlib.sha256(token.encode()).hexdigest())
    headers={'Authorization':'Bearer '+token}
    for path,method,body in [('/v1/leads','GET',None),('/ready','GET',None),('/v1/leads','POST',LEAD),('/v1/collect/federal-reserve','POST',None),('/v1/pipeline/run','POST',{'run_key':'test:scope'})]:
        assert client.request(method,path,headers=headers,json=body).status_code==403
    assert client.fake.calls==[]
    class IntakeStore(FakeStore):
        async def insert(self,table,body):return {'id':123}
        async def patch(self,*args):return []
    app.dependency_overrides[get_store]=IntakeStore
    body={'batch_id':'test:empty-intake','provider':'fixture','searched_at':'2026-11-03T12:00:00Z','queries':[{'id':'test','query':'public source'}],'hits':[],'run_limitations':'Synthetic local fixture'}
    r=client.post('/v1/search-intake',headers=headers,json=body)
    assert r.status_code==200 and r.json()['run_id']==123

def test_review_routes_remain_protected_and_missing_evidence_is_not_silently_created(client,monkeypatch):
    from test_analysis import review_payload
    import hashlib
    token='test-intake-only-key-'*3
    monkeypatch.setenv('SIGNAL_DESK_SEARCH_WRITER_KEY_SHA256',hashlib.sha256(token.encode()).hexdigest())
    assert client.get('/v1/clusters/1').status_code==401
    assert client.post('/v1/reviews',json=review_payload()).status_code==401
    assert client.post('/v1/reviews',headers={'Authorization':'Bearer '+token},json=review_payload()).status_code==403
    assert client.post('/v1/reviews',headers=AUTH,json=review_payload()).status_code==409
