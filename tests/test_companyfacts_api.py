"""API auth and source receipts: no SEC credentials means no pretend numbers."""
from datetime import datetime, timezone
import hashlib
from fastapi.testclient import TestClient
from app.main import app, requests
from app.market import PUBLIC_REQUESTS
from app.store import get_store
from test_market_companyfacts import raw_facts
from app.market_companyfacts import analyze_companyfacts


class FakeStore:
    def __init__(self):self.calls=[]
    async def request(self, method,path,**kwargs):
        self.calls.append(("request",method,path))
        if path=="market_fundamental_snapshots":
            return []
        raise AssertionError("Unexpected read")
    async def rpc(self,name,payload):
        self.calls.append(("rpc",name,payload))
        assert name=="market_record_fundamentals_v1"
        assert "portfolio" not in str(payload).lower()
        assert payload["payload"]["metrics"]["free_cash_flow_proxy"]["value"]=="270"
        return {"created":True,"snapshot_id":37,"orders_placed":False,
                "first_collected_at":payload["payload"]["source_collected_at"]}


def test_sec_owner_only_and_explicit_absence(monkeypatch):
    owner="fundamentals-owner-secret-"*3
    reader="fundamentals-reader-secret-"*3
    monkeypatch.setenv("SIGNAL_DESK_API_KEY",owner)
    monkeypatch.setenv("SIGNAL_DESK_READ_KEY_SHA256",hashlib.sha256(reader.encode()).hexdigest())
    monkeypatch.delenv("SEC_USER_AGENT",raising=False)
    store=FakeStore()
    requests.clear();PUBLIC_REQUESTS.clear()
    app.dependency_overrides[get_store]=lambda:store
    try:
        with TestClient(app) as client:
            assert client.get("/v1/market/company-fundamentals/NVDA").status_code==401
            assert client.post("/v1/market/company-fundamentals/NVDA/refresh").status_code==401
            headers={"Authorization":"Bearer "+reader}
            assert client.get("/v1/market/company-fundamentals/NVDA",headers=headers).status_code==403
            assert client.post("/v1/market/company-fundamentals/NVDA/refresh",headers=headers).status_code==403
            assert not store.calls
            owner_headers={"Authorization":"Bearer "+owner}
            latest=client.get("/v1/market/company-fundamentals/NVDA",headers=owner_headers)
            assert latest.json()["status"]=="NOT_COLLECTED"
            refreshed=client.post("/v1/market/company-fundamentals/NVDA/refresh",headers=owner_headers)
            assert refreshed.status_code==200
            assert refreshed.json()["status"]=="NOT_CONFIGURED"
            assert not any(x[0]=="rpc" for x in store.calls)
    finally:
        app.dependency_overrides.clear()


def test_verified_real_source_snapshot_is_persisted_without_account_data(monkeypatch):
    import app.market_companyfacts as module
    owner="fundamentals-owner-secret-"*3
    monkeypatch.setenv("SIGNAL_DESK_API_KEY",owner)
    requests.clear();PUBLIC_REQUESTS.clear()
    payload=analyze_companyfacts("NVDA",100,raw_facts(),
           captured_at=datetime.now(timezone.utc))
    payload["source_document_sha256"]="a"*64
    async def fake(ticker):
        assert ticker=="NVDA"
        return payload
    monkeypatch.setattr(module,"fetch_company_fundamentals",fake)
    store=FakeStore()
    app.dependency_overrides[get_store]=lambda:store
    try:
        with TestClient(app) as client:
            out=client.post("/v1/market/company-fundamentals/NVDA/refresh",
                            headers={"Authorization":"Bearer "+owner})
            assert out.status_code==200
            assert out.json()["storage"]["snapshot_id"]==37
            assert out.json()["metrics"]["revenue"]["accession"]=="0000000100-26-000001"
            assert out.json()["execution_enabled"] is False
            assert len([x for x in store.calls if x[0]=="rpc"])==1
    finally:
        app.dependency_overrides.clear()


def test_unknown_tickers_are_rejected_before_any_fetch(monkeypatch):
    owner="fundamentals-owner-secret-"*3
    monkeypatch.setenv("SIGNAL_DESK_API_KEY",owner)
    requests.clear();PUBLIC_REQUESTS.clear()
    store=FakeStore()
    app.dependency_overrides[get_store]=lambda:store
    try:
        with TestClient(app) as client:
            resp=client.post("/v1/market/company-fundamentals/FAKE/refresh",
                headers={"Authorization":"Bearer "+owner})
            assert resp.status_code==422
            assert not store.calls
    finally:
        app.dependency_overrides.clear()
