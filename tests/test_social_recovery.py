import asyncio
import json

import httpx

from app import social_recovery


def test_public_identity_check_and_no_raw_text_in_receipt(monkeypatch):
    url = "https://example.org/@public/123"
    async def fetch(endpoint):
        assert endpoint == "https://mastodon.social/api/v1/statuses/999"
        return json.dumps({"url":url,"visibility":"public","content":
            '<p>private-canary <a href="https://example.org/report">report</a></p>'})
    monkeypatch.setattr(social_recovery, "public_bytes", fetch)
    assert asyncio.run(social_recovery.retrieve({"source_url":url,"provider_item_id":"999"})) == (
        "public_text", ["https://example.org/report"], None)
    assert asyncio.run(social_recovery.retrieve({"source_url":url+"4","provider_item_id":"999"})) == (
        "unavailable", [], None)


def test_rate_limit_reaches_retry_rpc(monkeypatch):
    async def fetch(endpoint):
        request = httpx.Request("GET", endpoint)
        raise httpx.HTTPStatusError("rate limited", request=request,
            response=httpx.Response(429, request=request))
    monkeypatch.setattr(social_recovery, "public_bytes", fetch)
    class Store:
        async def rpc(self, name, body):
            if name == "signal_social_claim_v1":
                return [{"lead_id":1,"attempt":2,"source_url":"https://example.org/@public/123","provider_item_id":"123"}]
            if name == "signal_social_finish_v1":
                assert body == {"p_lead":1,"p_attempt":2,"p_outcome":"retryable","p_refs":[],"p_origin":None}
                return {"state":"retry"}
            return {"counts":{"retry":1}}
    result = asyncio.run(social_recovery.recover_social(Store()))
    assert result["receipts"] == [{"state":"retry"}]
    assert result["write_failures"] == 0
