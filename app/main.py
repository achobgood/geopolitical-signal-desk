import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from collections import deque

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.collector import collect
from app.models import AssessmentIn, DecisionIn, LeadIn, RelationIn, ClusterIn, PipelineIn
from app.store import StoreConflict, StoreUnavailable, get_store
from app.pipeline import run_pipeline
from app.providers import coverage
from app.search_intake import SearchBatch, import_search_batch
from app.analysis import EventReview, persist_review

app = FastAPI(title="Signal Desk", version="1.1.0", docs_url=None, redoc_url=None, openapi_url=None)
bearer = HTTPBearer(auto_error=False)
requests = deque()


async def authorize(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    expected = os.getenv("SIGNAL_DESK_API_KEY", "")
    expected_hash = os.getenv("SIGNAL_DESK_API_KEY_SHA256", "")
    if len(expected) < 32 and (len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash)):
        raise HTTPException(503, "API authentication is not configured")
    supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    reader_hash = os.getenv("SIGNAL_DESK_READ_KEY_SHA256", "")
    is_reader = len(reader_hash) == 64 and secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), reader_hash)
    writer_hash = os.getenv("SIGNAL_DESK_SEARCH_WRITER_KEY_SHA256", "")
    is_search_writer = len(writer_hash) == 64 and secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), writer_hash)
    valid = secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), expected_hash) if expected_hash else secrets.compare_digest(supplied.encode(), expected.encode())
    if not valid and not is_reader and not is_search_writer:
        raise HTTPException(401, "Invalid or missing API credentials", headers={"WWW-Authenticate": "Bearer"})
    if is_search_writer and not valid and (request.method != "POST" or request.url.path != "/v1/search-intake"):
        raise HTTPException(403, "This credential permits search intake only")
    if is_reader and not valid and request.method != "GET":
        raise HTTPException(403, "This credential permits read-only access")
    now = time.monotonic()
    while requests and requests[0] < now - 60:
        requests.popleft()
    if len(requests) >= 120:
        raise HTTPException(429, "Request limit reached", headers={"Retry-After": "60"})
    requests.append(now)
    return "api:shared-key" if valid else "api:search-writer" if is_search_writer else "api:reader"


@app.middleware("http")
async def boundaries(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex
    if request.method in {"POST", "PUT", "PATCH"}:
        # Count actual streamed bytes, not just an untrusted Content-Length header.
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(StoreUnavailable)
async def unavailable(request, exc):
    return JSONResponse({"detail": "Database unavailable or configuration incomplete", "request_id": request.state.request_id}, status_code=503)


@app.exception_handler(StoreConflict)
async def conflict(request, exc):
    return JSONResponse({"detail": "Record conflict or missing referenced lead"}, status_code=409)


@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    # Default validation errors echo submitted input; avoid reflecting sensitive payloads.
    return JSONResponse({"detail": [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}, status_code=422)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


async def ingest(lead: LeadIn, store, actor):
    payload = lead.model_dump(mode="json")
    # A permalink is an observation's identity. Repeat intake never overwrites it.
    key = "url-v1:" + fingerprint({"url": lead.source_url})
    return await store.rpc("signal_ingest_v1", {"payload": payload, "p_key": key, "p_actor": actor})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/openapi.json", dependencies=[Depends(authorize)])
async def openapi():
    return app.openapi()


@app.get("/ready", dependencies=[Depends(authorize)])
async def ready(store=Depends(get_store)):
    result = await store.rpc("signal_ready_v1", {})
    if result.get("schema_version") != 1 or result.get("rls_tables", 0) < 12 or result.get("end_user_policies") != 0:
        raise StoreUnavailable()
    return {"status": "ready", "schema_version": 1}


@app.post("/v1/leads")
async def create_lead(lead: LeadIn, actor=Depends(authorize), store=Depends(get_store)):
    return await ingest(lead, store, actor)


@app.post("/v1/search-intake")
async def search_intake(batch: SearchBatch,actor=Depends(authorize),store=Depends(get_store)):
    result=await import_search_batch(batch,store,ingest,actor)
    return JSONResponse(result,status_code=200 if result["status"]=="completed" else 503)


@app.get("/v1/leads", dependencies=[Depends(authorize)])
async def list_leads(limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, gt=0), store=Depends(get_store)):
    return await store.rows("leads", limit=limit, before=before)


@app.get("/v1/leads/{lead_id}", dependencies=[Depends(authorize)])
async def get_lead(lead_id: int, store=Depends(get_store)):
    rows = await store.rows("leads", filters={"id": f"eq.{lead_id}"})
    if not rows:
        raise HTTPException(404, "Lead not found")
    anchored=await store.rows("assessments",filters={"lead_id":f"eq.{lead_id}"})
    memberships=await store.request("GET","cluster_leads",params={"select":"cluster_id","lead_id":f"eq.{lead_id}","limit":"100"})
    cluster_ids=[r["cluster_id"] for r in memberships]
    linked=await store.rows("assessments",limit=100,filters={"cluster_id":"in.("+",".join(map(str,cluster_ids))+")"}) if cluster_ids else []
    assessments=list({a["id"]:a for a in anchored+linked}.values())
    return {"lead": rows[0], "evidence": await store.rows("evidence", filters={"lead_id": f"eq.{lead_id}"}),
        "assessments": assessments,
        "decisions": await store.rows("alert_history", filters={"lead_id": f"eq.{lead_id}"}),
        "relations": await store.rows("lead_relations", filters={"or": f"(lead_id.eq.{lead_id},other_lead_id.eq.{lead_id})"})}


@app.post("/v1/assessments")
async def assessment(value: AssessmentIn, actor=Depends(authorize), store=Depends(get_store)):
    payload = value.model_dump(mode="json")
    return await store.rpc("signal_record_v1", {"p_kind": "assessment", "payload": payload, "p_key": fingerprint(payload), "p_actor": actor})


@app.post("/v1/reviews")
async def review(value: EventReview, actor=Depends(authorize), store=Depends(get_store)):
    try:
        return await persist_review(value, store, actor)
    except ValueError:
        raise HTTPException(409, "Collect referenced evidence before reviewing it")


@app.get("/v1/clusters/{cluster_id}", dependencies=[Depends(authorize)])
async def cluster_detail(cluster_id: int, store=Depends(get_store)):
    rows=await store.rows("clusters", limit=1, filters={"id":f"eq.{cluster_id}"})
    if not rows:
        raise HTTPException(404,"Cluster not found")
    links=await store.request("GET","cluster_leads",params={"select":"*","cluster_id":f"eq.{cluster_id}","limit":"100"})
    ids=[r["lead_id"] for r in links]
    observations=await store.rows("leads",limit=100,filters={"id":"in.("+",".join(map(str,ids))+")"}) if ids else []
    reviews=await store.rows("assessments",limit=100,filters={"cluster_id":f"eq.{cluster_id}"})
    return {"cluster":rows[0],"linked_lead_ids":ids,"observations":observations,"assessments":reviews,
            "limitations":"At most 100 linked leads and assessment versions; source families are not independent confirmations. Decisions record intent; no external delivery."}


@app.post("/v1/decisions")
async def decision(value: DecisionIn, actor=Depends(authorize), store=Depends(get_store)):
    payload = value.model_dump(mode="json")
    return await store.rpc("signal_record_v1", {"p_kind": "decision", "payload": payload, "p_key": fingerprint(payload), "p_actor": actor})


@app.post("/v1/relations")
async def relation(value: RelationIn, actor=Depends(authorize), store=Depends(get_store)):
    return await store.rpc("signal_relation_v1", {"payload": value.model_dump(), "p_actor": actor})


@app.get("/v1/assessments", dependencies=[Depends(authorize)])
async def assessments(limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, gt=0), store=Depends(get_store)):
    return await store.rows("assessments", limit=limit, before=before)


@app.get("/v1/collection-runs", dependencies=[Depends(authorize)])
async def runs(limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, gt=0), store=Depends(get_store)):
    return await store.rows("collection_runs", limit=limit, before=before)


@app.get("/v1/audit-events", dependencies=[Depends(authorize)])
async def audits(limit: int = Query(50, ge=1, le=100), before: int | None = Query(None, gt=0), store=Depends(get_store)):
    return await store.rows("audit_events", limit=limit, before=before)


@app.get("/v1/desk/snapshot", dependencies=[Depends(authorize)])
async def desk_snapshot(store=Depends(get_store)):
    """One bounded read for the private UI; no derived confidence or fabricated scenarios."""
    tables = {"leads": 201, "assessments": 200, "clusters": 100, "lead_relations": 300,
              "collection_runs": 50, "activity_baselines": 100, "alert_history": 100}
    values = await asyncio.gather(*(store.rows(table, limit=limit) for table, limit in tables.items()))
    result = dict(zip(tables, values))
    result["cluster_leads"] = await store.request("GET", "cluster_leads", params={"select": "*", "order": "linked_at.desc", "limit": "500"})
    result["leads_has_more"] = len(result["leads"]) > 200
    result["leads"] = result["leads"][:200]
    result["limits"] = {**tables, "leads": 200, "cluster_leads": 500}
    result["coverage"] = coverage()
    result["pipeline_runs"] = await store.rows("pipeline_runs",limit=20)
    for item in result["coverage"]:
        item["last_run"] = next((r for r in result["collection_runs"] if r["collector"]==item["provider"]),None)
        item["scheduled_collection_observed"] = any(r["trigger_kind"]=="supabase_cron" and any(p["provider"]==item["provider"] and p["status"]=="completed" for p in r.get("details",{}).get("providers",[])) for r in result["pipeline_runs"])
        item["schedule_verified"] = item["scheduled_collection_observed"]
    result["scope_note"] = "Working multi-source collection and triage, not validated geopolitical prediction. Fed RSS records are ingestion proof of concept. Automated topic queues are not event conclusions."
    return result


@app.get("/v1/social-recovery", dependencies=[Depends(authorize)])
async def social_recovery_status(store=Depends(get_store)):
    return await store.rpc("signal_social_status_v1", {})


@app.get("/v1/providers", dependencies=[Depends(authorize)])
async def providers():
    return coverage()


@app.post("/v1/clusters")
async def cluster(value: ClusterIn, actor=Depends(authorize), store=Depends(get_store)):
    return await store.rpc("signal_cluster_v1", {"payload":value.model_dump(),"p_actor":actor})


@app.post("/v1/pipeline/run")
async def pipeline(value: PipelineIn, actor=Depends(authorize), store=Depends(get_store)):
    return await run_pipeline(store,ingest,value.run_key,"manual:"+actor)


@app.post("/v1/pipeline/scheduled")
async def scheduled(value: PipelineIn, credentials: HTTPAuthorizationCredentials | None=Depends(bearer), store=Depends(get_store)):
    expected=os.getenv("SIGNAL_DESK_SCHEDULER_KEY_SHA256","")
    supplied=credentials.credentials if credentials and credentials.scheme.lower()=="bearer" else ""
    if len(expected)!=64 or not secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(),expected):
        raise HTTPException(401,"Invalid scheduler credential")
    result=await run_pipeline(store,ingest,value.run_key,"supabase_cron")
    return JSONResponse(result,status_code=200 if result["run"]["run_status"]=="completed" else 207)


@app.post("/v1/collect/federal-reserve", dependencies=[Depends(authorize)])
async def collect_fed(store=Depends(get_store)):
    result = await collect(store, ingest)
    return JSONResponse(result, status_code=200 if result["run_status"] == "completed" else 502)


# Additive market domain. Existing geopolitical routes and access controls remain unchanged.
from app.market import register_market_routes
register_market_routes(app, authorize)
