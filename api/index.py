import os
import time
import uuid
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Medical Universal Search")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ABDM_BASE = "https://drugregistrysbx.abdm.gov.in/drug-registry/v1"
SESSION_URL = "https://live.abdm.gov.in/api/hiecm/gateway/v3/sessions"

# Module-level token cache (survives warm lambda re-use)
_cache = {"token": None, "fetched_at": 0.0}


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def get_access_token() -> str:
    """
    Token priority:
    1. Module-level cache (< 15 min old)
    2. ABDM session API using CLIENT_ID + CLIENT_SECRET env vars
    3. ABDM_ACCESS_TOKEN env var (manually set / pushed by token_refresh.py)
    """
    age = time.time() - _cache["fetched_at"]
    if _cache["token"] and age < 840:   # 14 min
        return _cache["token"]

    client_id = os.environ.get("CLIENT_ID")
    client_secret = os.environ.get("CLIENT_SECRET")

    if client_id and client_secret:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                SESSION_URL,
                json={
                    "clientId": client_id,
                    "clientSecret": client_secret,
                    "grantType": "client_credentials",
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "REQUEST-ID": str(uuid.uuid4()),
                    "TIMESTAMP": _iso_now(),
                    "X-CM-ID": "sbx",
                },
            )
            if resp.status_code == 200:
                token = resp.json().get("accessToken", "")
                _cache["token"] = token
                _cache["fetched_at"] = time.time()
                return token

    # Fallback: env var pushed by local token_refresh.py
    token = os.environ.get("ABDM_ACCESS_TOKEN", "")
    if token:
        _cache["token"] = token
        _cache["fetched_at"] = time.time()
        return token

    raise HTTPException(status_code=503, detail="No auth token available. Set CLIENT_ID+CLIENT_SECRET or ABDM_ACCESS_TOKEN env vars.")


def _headers(token: str) -> dict:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_drugs(
    q: str = Query(..., description="Drug / brand name"),
    page: int = Query(0),
    limit: int = Query(10, le=50),
):
    token = await get_access_token()
    url = f"{ABDM_BASE}/search"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, params={"q": q, "page": page, "limit": limit}, headers=_headers(token))
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/brand/{brand_id}")
async def get_brand(brand_id: str):
    token = await get_access_token()
    url = f"{ABDM_BASE}/brand/{brand_id}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=_headers(token))
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/generic/{generic_id}")
async def get_generic(generic_id: str):
    token = await get_access_token()
    url = f"{ABDM_BASE}/generics/{generic_id}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=_headers(token))
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/supplier/{supplier_id}")
async def get_supplier(
    supplier_id: str,
    page: int = Query(0),
    limit: int = Query(10, le=50),
):
    token = await get_access_token()
    url = f"{ABDM_BASE}/suppliers/{supplier_id}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, params={"page": page, "limit": limit}, headers=_headers(token))
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/substance/{substance_id}")
async def get_substance(substance_id: str):
    token = await get_access_token()
    url = f"{ABDM_BASE}/substances/{substance_id}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=_headers(token))
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/health")
async def health():
    return {"status": "ok"}
