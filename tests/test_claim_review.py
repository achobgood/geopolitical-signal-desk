import asyncio
import json

import httpx

from app import claim_review
from app.claim_review import ModelJudgment, build_review, run_claim_review


OBSERVATION = "The official report says relief distribution resumed the next day."
JUDGMENT = {"disposition":"changed","dimension":"timing","lead_id":1,
            "excerpt":OBSERVATION,"reason_code":"new_fact","reason":"New timing detail."}


def sample():
    return {"cluster_id":2,"event_key":"test-event","title":"Historical relief case",
            "broad_region":"Europe","prior_summary":"A relief site reported a disruption.",
            "captured_at":"2026-11-03T22:00:00Z","prior_assessments":[],
            "open_questions":["When did distribution resume?"],
            "observations":[{"id":1,"url":"https://ukraine.un.org/en/example",
                "observation":OBSERVATION,"source_kind":"official","content_access":"text_inspected",
                "safety_reviewed":True,"link_basis":"analyst_reviewed"}]}


def test_rejects_unsafe_or_unbound_change():
    j = ModelJudgment(**JUDGMENT)
    p = sample()
    assert build_review(p, [1], j, "test-key").hypotheses[0].supporting_urls == [p["observations"][0]["url"]]
    assert build_review(p, [], j, "test-key") is None
    assert build_review(p, [1], j.model_copy(update={"excerpt":"This fabricated detail is absent."}), "test-key") is None
    p["observations"][0]["link_basis"] = "other"
    assert build_review(p, [1], j, "test-key") is None


def test_scheduled_model_response_reaches_validated_writeback(monkeypatch):
    monkeypatch.setenv("SIGNAL_DESK_MODEL_REVIEW", "publish")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    real_client = httpx.AsyncClient

    async def handle(request):
        assert request.url.host == "api.openai.com"
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert "fixture-only" not in body["input"]
        return httpx.Response(200, json={"status":"completed","id":"fixture",
            "output":[{"type":"message","content":[{"type":"output_text","text":json.dumps(JUDGMENT)}]}]})

    monkeypatch.setattr(claim_review.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))

    class Store:
        async def rows(self, *args, **kwargs):
            return []

    async def save(review, store, actor):
        assert actor == "pipeline:model-claim-review"
        assert review.event_key == "test-event"
        return {"assessment_ids":[4],"cluster_id":2,"notification_delivered":False}

    monkeypatch.setattr(claim_review, "persist_review", save)
    receipt = asyncio.run(run_claim_review(Store(), {"packets":[sample()],
        "dispositions":[{"cluster_id":2,"outcome":"review_required","pending_review_ids":[1]}]}, 10))
    assert receipt["status"] == "assessment_saved"
    assert receipt["review_receipt"]["assessment_ids"] == [4]
