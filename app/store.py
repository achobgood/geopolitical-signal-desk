"""Server-only PostgREST adapter. No credentials or upstream response bodies in logs."""
import os
import httpx


class StoreUnavailable(Exception):
    pass


class StoreConflict(Exception):
    pass


class Store:
    def __init__(self):
        self.url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    async def request(self, method, path, *, params=None, body=None):
        # Bind credentials to the intended Supabase project, never a request-supplied host.
        if self.url != "https://uzqpndpuqubfygjwnhiv.supabase.co" or not self.key:
            raise StoreUnavailable()
        headers = {"apikey": self.key, "Prefer": "return=representation"}
        # Modern secret keys are API gateway keys, not JWT bearer tokens.
        if not self.key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.key}"
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
                r = await client.request(method, f"{self.url}/rest/v1/{path}", headers=headers, params=params, json=body)
            if r.status_code == 409:
                raise StoreConflict()
            if r.status_code >= 300:
                raise StoreUnavailable()
            return r.json() if r.content else None
        except (httpx.HTTPError, ValueError) as exc:
            raise StoreUnavailable() from exc

    async def rpc(self, name, body):
        return await self.request("POST", f"rpc/{name}", body=body)

    async def rows(self, table, *, limit=50, before=None, filters=None):
        params = {"select": "*", "order": "id.desc", "limit": str(limit)}
        if before is not None:
            params["id"] = f"lt.{before}"
        params.update(filters or {})
        return await self.request("GET", table, params=params)

    async def insert(self, table, body):
        return (await self.request("POST", table, body=body))[0]

    async def patch(self, table, row_id, body):
        return await self.request("PATCH", table, params={"id": f"eq.{row_id}"}, body=body)


def get_store():
    return Store()
