import asyncio
import time
import threading
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright

ABDM_BASE = "https://drugregistrysbx.abdm.gov.in/drug-registry/v1"
PORTAL_URL = "https://drugregistrysbx.abdm.gov.in/"

_token: dict = {"bearer": "", "api_key": "", "fetched_at": 0.0}
_lock = threading.Lock()


# ─── Token refresh via headless browser ───────────────────────────────────────

def _fetch_tokens_via_browser() -> dict:
    captured = {}

    def on_request(req):
        h = req.headers
        if "apikey" in h and "api_key" not in captured:
            captured["api_key"] = h["apikey"]
        if "authorization" in h and "bearer" not in captured:
            captured["bearer"] = h["authorization"]  # includes "Bearer " prefix

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.on("request", on_request)
        page.goto(PORTAL_URL)
        page.wait_for_timeout(8000)
        browser.close()

    return captured


def _refresh_token():
    print("[token] Refreshing via headless browser…", flush=True)
    try:
        tokens = _fetch_tokens_via_browser()
        if tokens.get("bearer") and tokens.get("api_key"):
            with _lock:
                _token["bearer"] = tokens["bearer"]
                _token["api_key"] = tokens["api_key"]
                _token["fetched_at"] = time.time()
            print(f"[token] OK — bearer={tokens['bearer'][:30]}…", flush=True)
        else:
            print(f"[token] WARNING: incomplete tokens {list(tokens.keys())}", flush=True)
    except Exception as e:
        print(f"[token] ERROR: {e}", flush=True)


def _background_loop():
    _refresh_token()
    while True:
        time.sleep(14 * 60)
        _refresh_token()


def _get_headers() -> dict:
    with _lock:
        bearer = _token["bearer"]
        api_key = _token["api_key"]
    if not bearer:
        raise HTTPException(status_code=503, detail="Token not ready yet — retry in a few seconds.")
    return {
        "Accept": "application/json",
        "Authorization": bearer,
        "Apikey": api_key,
    }


# ─── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_background_loop, daemon=True).start()
    yield


app = FastAPI(title="Medical Universal Search", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Static frontend ──────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

@app.head("/")
async def health_head():
    from fastapi.responses import Response
    return Response(status_code=200)


# ─── Helpers ─────────────────────────────────────────────────────────────────

PAGE_SIZE = 50      # max per request to the upstream API
MAX_RESULTS = 500   # cap to avoid very long fetches (500 = 10 concurrent pages)

async def _fetch_all_pages(client: httpx.AsyncClient, url: str, base_params: dict, data_key: str) -> tuple[list, int]:
    """
    Fetch page 0 to learn total count, then fetch all remaining pages
    concurrently. Returns (all_items, total_count).
    """
    # Page 0
    r0 = await client.get(url, params={**base_params, "page": 0, "limit": PAGE_SIZE}, headers=_get_headers())
    if r0.status_code != 200:
        raise HTTPException(status_code=r0.status_code, detail=r0.text)
    body0 = r0.json()
    items = list(body0.get(data_key, []))
    total = body0.get("count") or body0.get("drugsCount") or len(items)

    # How many more pages do we need?
    remaining_items = min(total, MAX_RESULTS) - len(items)
    if remaining_items <= 0:
        return items, total

    pages_needed = (remaining_items + PAGE_SIZE - 1) // PAGE_SIZE
    tasks = [
        client.get(url, params={**base_params, "page": p + 1, "limit": PAGE_SIZE}, headers=_get_headers())
        for p in range(pages_needed)
    ]
    responses = await asyncio.gather(*tasks)
    for resp in responses:
        if resp.status_code == 200:
            items.extend(resp.json().get(data_key, []))

    return items[:MAX_RESULTS], total


# ─── API routes ───────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_drugs(q: str = Query(...)):
    async with httpx.AsyncClient(timeout=60) as client:
        items, total = await _fetch_all_pages(client, f"{ABDM_BASE}/search", {"q": q}, "drugDetails")
    return {"drugDetails": items, "count": total, "returned": len(items)}


async def _fetch_all_alternates(client: httpx.AsyncClient, url: str) -> dict:
    """Fetch all paginated alternateDrugs for a brand or generic endpoint, deduped by brandIdentifier."""
    r0 = await client.get(url, params={"page": 0}, headers=_get_headers())
    if r0.status_code != 200:
        raise HTTPException(status_code=r0.status_code, detail=r0.text)
    body0 = r0.json()
    total = body0.get("totalCount", 0)
    per_page = len(body0.get("alternateDrugs", [])) or 25

    pages_needed = max(0, (min(total, MAX_RESULTS) - per_page + per_page - 1) // per_page)
    extra_pages: list = []
    if pages_needed > 0:
        tasks = [client.get(url, params={"page": p + 1}, headers=_get_headers()) for p in range(pages_needed)]
        for resp in await asyncio.gather(*tasks):
            if resp.status_code == 200:
                extra_pages.extend(resp.json().get("alternateDrugs", []))

    # Deduplicate by brandIdentifier, preserving order
    seen: set = set()
    deduped = []
    for item in (body0.get("alternateDrugs", []) + extra_pages):
        key = item.get("brandIdentifier")
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)

    body0["alternateDrugs"] = deduped[:MAX_RESULTS]
    body0["totalCount"] = total
    return body0


@app.get("/api/brand/{brand_id}")
async def get_brand(brand_id: str):
    async with httpx.AsyncClient(timeout=60) as client:
        return await _fetch_all_alternates(client, f"{ABDM_BASE}/brand/{brand_id}")


@app.get("/api/generic/{generic_id}")
async def get_generic(generic_id: str):
    async with httpx.AsyncClient(timeout=60) as client:
        return await _fetch_all_alternates(client, f"{ABDM_BASE}/generics/{generic_id}")


@app.get("/api/supplier/{supplier_id}")
async def get_supplier(supplier_id: str):
    async with httpx.AsyncClient(timeout=60) as client:
        items, total = await _fetch_all_pages(
            client, f"{ABDM_BASE}/suppliers/{supplier_id}", {}, "drugDetails"
        )
        # Also fetch supplier meta from the first page (already done inside helper — re-fetch for details)
        r0 = await client.get(
            f"{ABDM_BASE}/suppliers/{supplier_id}",
            params={"page": 0, "limit": 1},
            headers=_get_headers(),
        )
    supplier_details = r0.json().get("supplierDetails", {}) if r0.status_code == 200 else {}
    return {"drugDetails": items, "drugsCount": total, "returned": len(items), "supplierDetails": supplier_details}


@app.get("/api/substance/{substance_id}")
async def get_substance(substance_id: str):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{ABDM_BASE}/substances/{substance_id}", headers=_get_headers())
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/health")
async def health():
    age = round(time.time() - _token["fetched_at"])
    return {
        "status": "ok",
        "token_age_seconds": age,
        "token_present": bool(_token["bearer"]),
    }
