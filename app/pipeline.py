"""Collection -> persistent topic correlations -> bounded triage; no alert delivery."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import httpx

from app.models import AssessmentIn, ClusterIn, RelationIn
from app.providers import PROVIDERS
from app.social_recovery import recover_social


def now():return datetime.now(timezone.utc).isoformat()
def key(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


async def provider_run(provider, store, ingest):
    cap=provider.capabilities
    prior=await store.rows("collection_runs",limit=1,filters={"collector":"eq."+cap.name,"run_status":"eq.completed"})
    previous=prior[0] if prior else {}
    if previous.get("finished_at") and (datetime.now(timezone.utc)-datetime.fromisoformat(previous["finished_at"].replace("Z","+00:00"))).total_seconds()<cap.min_interval_seconds:
        return {"provider":cap.name,"status":"paced","new_leads":0,"lead_ids":[]}
    run=await store.insert("collection_runs",{"collector":cap.name,"access_limitations":cap.limitations})
    seen=new=0;ids=[];status="completed";error="";details={}
    try:
        batch=await provider.fetch(previous.get("details",{}).get("cursor",{}))
        seen=batch.seen;details={"cursor":batch.cursor,"dropped":batch.dropped,"collection_threshold":"Permitted bounded public metadata/headlines; no corroboration gate"}
        for lead in batch.leads:
            result=await ingest(lead,store,"collector:"+cap.name)
            ids.append(result["lead"]["id"]);new+=int(result["created"])
    except Exception as exc:
        status="partial" if ids else "failed"
        if isinstance(exc,httpx.HTTPStatusError):error=f"Provider returned HTTP {exc.response.status_code}; no restriction bypass attempted"
        elif isinstance(exc,(httpx.TimeoutException,TimeoutError)):error="Provider timed out; retry remains bounded by pacing"
        else:error="Provider parse/ingest failed; no credential or content details logged"
    finished={"run_status":status,"finished_at":now(),"observations_seen":seen,"new_leads":new,"error_summary":error,"details":details}
    await store.patch("collection_runs",run["id"],finished)
    return {"provider":cap.name,"run_id":run["id"],"status":status,"new_leads":new,"seen":seen,"lead_ids":ids}


async def correlate_and_triage(store):
    leads=await store.rows("leads",limit=300)
    relevant=[l for l in leads if l.get("provenance",{}).get("topic") not in (None,"unspecified")]
    groups={};origins={};links=0;triage=0
    for lead in relevant:
        p=lead["provenance"];groups.setdefault(p["topic"],[]).append(lead)
        origin=p.get("origin_group","unknown")
        if origin!="unknown" and not origin.startswith("UN News;"):origins.setdefault(origin,[]).append(lead)
    for origin,items in origins.items():
        for other in items[1:]:
            a,b=sorted([items[0]["id"],other["id"]])
            payload=RelationIn(lead_id=a,other_lead_id=b,relation_type="same_origin",rationale="Recorded origin group matches: "+origin).model_dump()
            r=await store.rpc("signal_relation_v1",{"payload":payload,"p_actor":"pipeline:origin-trace"});links+=int(r["created"])
    for topic,items in groups.items():
        payload=ClusterIn(canonical_key="topic-review:"+key(topic),title="Topic review: "+topic,broad_region=items[0]["broad_region"],
            scenario="Automated topic queue only. Group membership is not evidence of a shared event, corroboration or escalation.",
            competing_explanations="Unrelated events, reposts, commentary or routine conditions; analyst review required.",lead_ids=[l["id"] for l in items[:100]]).model_dump()
        await store.rpc("signal_cluster_v1",{"payload":payload,"p_actor":"pipeline:topic-review"})
    for lead in [l for l in relevant if l["source_platform"]=="mastodon_public"][:3]:
        payload=AssessmentIn(lead_id=lead["id"],assessment_origin="automated_triage",hypothesis_key="needs-source-review-v1",
            scenario="Public-social discovery requires original-source review; no event conclusion",confidence="low",
            rationale="Only public metadata was retained. This triage task does not assert the post's claim, firsthand status or independence.",
            alternative_explanations="Commentary, same-origin repost, unrelated or false allegation; media inaccessible/uninspected",
            falsification_criteria="Inspect the original source and trace references; reject any event hypothesis unsupported by inspected evidence",
            analysis_threshold="Single public lead is sufficient for triage. Corroboration is not required to retain it.",
            source_limitations="Post text/media were not retained or inspected by the collector; no event or escalation assessment",
            strengthening_conditions="A permitted original-source inspection and documented provenance may support a later analyst assessment").model_dump(mode="json")
        r=await store.rpc("signal_record_v1",{"p_kind":"assessment","payload":payload,"p_key":key(payload),"p_actor":"pipeline:automated-triage"});triage+=int(r["created"])
    return {"topic_queues":len(groups),"new_same_origin_relations":links,"new_triage_tasks":triage,"notification_decision":"No notification: analyst event/impact threshold not established"}


async def run_pipeline(store,ingest,run_key,trigger):
    lease=await store.rpc("signal_pipeline_claim_v1",{"p_key":run_key,"p_trigger":trigger})
    if not lease["claimed"]:return {"duplicate":True,"run":lease["run"]}
    run=lease["run"];results=[];analysis={};status="completed";error=""
    try:
        # Sequential provider pacing bounds both outbound calls and database concurrency.
        async with asyncio.timeout(150):
            for provider in PROVIDERS.values():results.append(await provider_run(provider,store,ingest))
            social = await recover_social(store)
            analysis = {"social_recovery": social}
            analysis.update(await correlate_and_triage(store))
            if social["write_failures"]:
                status = "partial"
        if any(r["status"] in ("failed","partial") for r in results):status="partial"
    except Exception:
        status="partial" if results else "failed";error="Pipeline did not complete; inspect run history and provider limitations"
    finished={"finished_at":now(),"run_status":status,"details":{"providers":results,"analysis":analysis,"error":error}}
    await store.patch("pipeline_runs",run["id"],finished)
    return {"duplicate":False,"run":{**run,**finished}}
