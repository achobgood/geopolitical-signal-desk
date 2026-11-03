"""Bounded public retrieval, followed by exact references to saved evidence."""
import asyncio
from html.parser import HTMLParser
import json
import re

import httpx

from app.providers import public_bytes


class PostText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.has_text = False
        self.links = []

    def handle_data(self, text):
        self.has_text |= bool(text.strip())

    def handle_starttag(self, tag, attributes):
        if tag == "a":
            href = dict(attributes).get("href") or ""
            if href.startswith("https://") and len(href) <= 2000:
                self.links.append(href)


async def retrieve(job):
    """Fetch through fixed public API hosts. Never follow post-supplied URLs."""
    url = job["source_url"]
    target = url.removeprefix("https://fed.brid.gy/r/")
    if target != url:
        match = re.fullmatch(r"https://bsky\.app/profile/(did:plc:[a-z2-7]+)/post/([a-z0-9]+)", target)
        if not match:
            return "publisher_pointer", [target] if target.startswith("https://") else [], None
        uri = f"at://{match[1]}/app.bsky.feed.post/{match[2]}"
        posts = json.loads(await public_bytes(
            "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts?uris=" + uri
        )).get("posts", [])
        if not posts or posts[0].get("uri") != uri:
            return "unavailable", [], None
        record = posts[0]["record"]
        refs = [f["uri"] for facet in record.get("facets", []) for f in facet.get("features", [])
                if f.get("$type") == "app.bsky.richtext.facet#link" and f.get("uri")]
        embed = record.get("embed", {})
        external = embed.get("external", {}).get("uri")
        if external:
            refs.append(external)
        return ("public_text" if record.get("text", "").strip() else "unavailable"), refs[:20], target

    item_id = job.get("provider_item_id") or ""
    if not re.fullmatch(r"[0-9]{1,24}", item_id):
        return "unavailable", [], None
    post = json.loads(await public_bytes("https://mastodon.social/api/v1/statuses/" + item_id))
    if post.get("visibility") != "public" or post.get("url") != url:
        return "unavailable", [], None
    upstream = post.get("reblog")
    original = upstream or post
    if original.get("visibility") != "public":
        return "unavailable", [], None
    parser = PostText()
    parser.feed(original.get("content", ""))
    return ("public_text" if parser.has_text else "unavailable"), parser.links[:20], (
        original.get("url") if upstream else None
    )


async def recover_social(store):
    jobs = await store.rpc("signal_social_claim_v1", {"p_limit": 8})

    async def attempt(job):
        try:
            outcome, refs, origin = await retrieve(job)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            outcome, refs, origin = ("retryable" if code in (408, 429) or code >= 500 else "unavailable"), [], None
        except (httpx.RequestError, TimeoutError, ValueError, KeyError, TypeError):
            outcome, refs, origin = "retryable", [], None
        return await store.rpc("signal_social_finish_v1", {
            "p_lead": job["lead_id"], "p_attempt": job["attempt"],
            "p_outcome": outcome, "p_refs": refs, "p_origin": origin,
        })

    results = await asyncio.gather(*(attempt(job) for job in jobs), return_exceptions=True)
    receipts = [result for result in results if not isinstance(result, BaseException)]
    return {"version": 1, "attempted": len(jobs), "receipts": receipts,
            "write_failures": len(jobs) - len(receipts),
            "backlog": await store.rpc("signal_social_status_v1", {})}
