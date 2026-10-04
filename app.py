import asyncio
import re
import time
import threading
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware

ABDM_BASE    = "https://drugregistrysbx.abdm.gov.in/drug-registry/v1"
PORTAL_BASE  = "https://drugregistrysbx.abdm.gov.in"
JS_PATH_RE   = re.compile(r"/dr/v3/static/js/main\.[a-f0-9]+\.js")
UA           = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

_token: dict = {"bearer": "", "api_key": "", "fetched_at": 0.0}
_lock = threading.Lock()

PAGE_SIZE   = 50
MAX_RESULTS = 500


# ─── Token refresh via HTTP (no browser needed) ───────────────────────────────

async def _find_js_bundle_path(c: httpx.AsyncClient, h: dict) -> str:
    """Return the path to the main JS bundle, trying multiple strategies."""
    # Strategy 1: asset-manifest.json (most reliable — a static JSON file)
    try:
        resp = await c.get(f"{PORTAL_BASE}/dr/v3/asset-manifest.json", headers=h)
        if resp.status_code == 200:
            files = resp.json().get("files", {})
            path = next(
                (v for k, v in files.items()
                 if "main" in k and k.endswith(".js") and "chunk" not in k),
                None,
            )
            if path:
                print(f"[token] JS bundle via asset-manifest: {path}", flush=True)
                return path
    except Exception as e:
        print(f"[token] asset-manifest failed: {e}", flush=True)

    # Strategy 2: fetch /dr/v3/index.html directly
    for url in (f"{PORTAL_BASE}/dr/v3/index.html", f"{PORTAL_BASE}/dr/v3"):
        try:
            resp = await c.get(url, headers={**h, "Accept": "text/html,*/*"})
            m = JS_PATH_RE.search(resp.text)
            if m:
                print(f"[token] JS bundle via HTML ({url}): {m.group(0)}", flush=True)
                return m.group(0)
        except Exception as e:
            print(f"[token] HTML fetch {url} failed: {e}", flush=True)

    print("[token] Could not find JS bundle path via any strategy", flush=True)
    return ""


async def _fetch_tokens_via_http() -> dict:
    """
    1. Locate the portal JS bundle via asset-manifest.json or HTML scraping.
    2. Extract the hardcoded apikey from the bundle.
    3. GET /uma/sessions with that apikey → returns accessToken (bearer).
    """
    h = {"User-Agent": UA, "Accept": "application/json,text/html,*/*"}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=h) as c:
        js_path = await _find_js_bundle_path(c, h)
        if not js_path:
            return {}

        js_url = PORTAL_BASE + js_path
        js_text = (await c.get(js_url)).text

        # Step 2: extract apikey — stored as  apikey:"eyJ..."  in the minified bundle
        key_m = re.search(r'apikey:"([^"]+)"', js_text)
        if not key_m:
            print("[token] apikey not found in JS bundle", flush=True)
            return {}
        api_key = key_m.group(1)

        # Step 3: GET /uma/sessions → { "accessToken": "eyJ..." }
        sess = await c.get(
            f"{PORTAL_BASE}/drug-registry/v1/uma/sessions",
            headers={**h, "Apikey": api_key},
        )
        if sess.status_code != 200:
            print(f"[token] /uma/sessions {sess.status_code}: {sess.text[:150]}", flush=True)
            return {}

        access_token = sess.json().get("accessToken", "")
        if not access_token:
            print(f"[token] no accessToken in response: {sess.text[:150]}", flush=True)
            return {}

        return {"api_key": api_key, "bearer": f"Bearer {access_token}"}


def _refresh_token():
    print("[token] Refreshing via HTTP…", flush=True)
    try:
        tokens = asyncio.run(_fetch_tokens_via_http())
        if tokens.get("bearer") and tokens.get("api_key"):
            with _lock:
                _token["bearer"]     = tokens["bearer"]
                _token["api_key"]    = tokens["api_key"]
                _token["fetched_at"] = time.time()
            print(f"[token] OK — {tokens['bearer'][:40]}…", flush=True)
        else:
            print("[token] WARNING: could not obtain tokens", flush=True)
    except Exception as e:
        print(f"[token] ERROR: {e}", flush=True)


def _background_loop():
    _refresh_token()
    while True:
        time.sleep(14 * 60)
        _refresh_token()


def _get_headers() -> dict:
    with _lock:
        bearer  = _token["bearer"]
        api_key = _token["api_key"]
    if not bearer:
        raise HTTPException(status_code=503, detail="Token not ready — retry in a few seconds.")
    return {"Accept": "application/json", "Authorization": bearer, "Apikey": api_key}


# ─── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_background_loop, daemon=True).start()
    yield


app = FastAPI(title="Medical Universal Search", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─── Static frontend ──────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

@app.head("/")
async def health_head():
    return Response(status_code=200)


# ─── Pagination helpers ───────────────────────────────────────────────────────

async def _fetch_all_pages(client: httpx.AsyncClient, url: str, base_params: dict, data_key: str) -> tuple[list, int]:
    r0 = await client.get(url, params={**base_params, "page": 0, "limit": PAGE_SIZE}, headers=_get_headers())
    if r0.status_code != 200:
        raise HTTPException(status_code=r0.status_code, detail=r0.text)
    body0   = r0.json()
    items   = list(body0.get(data_key, []))
    total   = body0.get("count") or body0.get("drugsCount") or len(items)

    remaining  = min(total, MAX_RESULTS) - len(items)
    if remaining <= 0:
        return items, total

    pages = (remaining + PAGE_SIZE - 1) // PAGE_SIZE
    tasks = [client.get(url, params={**base_params, "page": p + 1, "limit": PAGE_SIZE}, headers=_get_headers()) for p in range(pages)]
    for resp in await asyncio.gather(*tasks):
        if resp.status_code == 200:
            items.extend(resp.json().get(data_key, []))

    return items[:MAX_RESULTS], total


async def _fetch_all_alternates(client: httpx.AsyncClient, url: str) -> dict:
    r0       = await client.get(url, params={"page": 0}, headers=_get_headers())
    if r0.status_code != 200:
        raise HTTPException(status_code=r0.status_code, detail=r0.text)
    body0    = r0.json()
    total    = body0.get("totalCount", 0)
    per_page = len(body0.get("alternateDrugs", [])) or 25

    pages    = max(0, (min(total, MAX_RESULTS) - per_page + per_page - 1) // per_page)
    extra: list = []
    if pages > 0:
        tasks = [client.get(url, params={"page": p + 1}, headers=_get_headers()) for p in range(pages)]
        for resp in await asyncio.gather(*tasks):
            if resp.status_code == 200:
                extra.extend(resp.json().get("alternateDrugs", []))

    seen, deduped = set(), []
    for item in body0.get("alternateDrugs", []) + extra:
        k = item.get("brandIdentifier")
        if k and k not in seen:
            seen.add(k); deduped.append(item)

    body0["alternateDrugs"] = deduped[:MAX_RESULTS]
    body0["totalCount"]     = total
    return body0


# ─── API routes ───────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_drugs(q: str = Query(...)):
    async with httpx.AsyncClient(timeout=60) as c:
        items, total = await _fetch_all_pages(c, f"{ABDM_BASE}/search", {"q": q}, "drugDetails")
    return {"drugDetails": items, "count": total, "returned": len(items)}


@app.get("/api/brand/{brand_id}")
async def get_brand(brand_id: str):
    async with httpx.AsyncClient(timeout=60) as c:
        return await _fetch_all_alternates(c, f"{ABDM_BASE}/brand/{brand_id}")


@app.get("/api/generic/{generic_id}")
async def get_generic(generic_id: str):
    async with httpx.AsyncClient(timeout=60) as c:
        return await _fetch_all_alternates(c, f"{ABDM_BASE}/generics/{generic_id}")


@app.get("/api/supplier/{supplier_id}")
async def get_supplier(supplier_id: str):
    async with httpx.AsyncClient(timeout=60) as c:
        items, total = await _fetch_all_pages(c, f"{ABDM_BASE}/suppliers/{supplier_id}", {}, "drugDetails")
        r0 = await c.get(f"{ABDM_BASE}/suppliers/{supplier_id}", params={"page": 0, "limit": 1}, headers=_get_headers())
    meta = r0.json().get("supplierDetails", {}) if r0.status_code == 200 else {}
    return {"drugDetails": items, "drugsCount": total, "returned": len(items), "supplierDetails": meta}


@app.get("/api/substance/{substance_id}")
async def get_substance(substance_id: str):
    async with httpx.AsyncClient(timeout=20) as c:
        resp = await c.get(f"{ABDM_BASE}/substances/{substance_id}", headers=_get_headers())
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/health")
async def health():
    return {"status": "ok", "token_age_seconds": round(time.time() - _token["fetched_at"]), "token_present": bool(_token["bearer"])}
