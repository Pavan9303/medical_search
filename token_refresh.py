"""
Run this locally every ~14 minutes to push a fresh ABDM access token to Vercel.

Usage:
  python token_refresh.py

Requires:
  pip install playwright requests
  playwright install chromium

Environment variables (set once):
  VERCEL_TOKEN   - your Vercel personal access token
  VERCEL_PROJECT_ID - your Vercel project ID (from project settings)
  VERCEL_TEAM_ID    - optional, only for team projects
"""

import os
import time
import requests
from playwright.sync_api import sync_playwright

DRUG_REGISTRY_URL = (
    "https://drugregistrysbx.abdm.gov.in/drug-registry/v1/search"
    "?q=paracetamol&page=0&limit=1"
)

VERCEL_TOKEN = os.environ.get("VERCEL_TOKEN", "")
VERCEL_PROJECT_ID = os.environ.get("VERCEL_PROJECT_ID", "")
VERCEL_TEAM_ID = os.environ.get("VERCEL_TEAM_ID", "")


def fetch_token_from_browser() -> str:
    """Intercept the ABDM access token from the drug registry network requests."""
    token = {"value": ""}

    def on_request(req):
        auth = req.headers.get("authorization", "")
        if auth.startswith("Bearer ") and not token["value"]:
            token["value"] = auth  # keep full "Bearer <token>"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("request", on_request)
        page.goto(DRUG_REGISTRY_URL)
        page.wait_for_timeout(6000)
        browser.close()

    return token["value"].removeprefix("Bearer ").strip()


def push_to_vercel(token_value: str):
    """Update the ABDM_ACCESS_TOKEN environment variable on Vercel."""
    if not VERCEL_TOKEN or not VERCEL_PROJECT_ID:
        print("VERCEL_TOKEN / VERCEL_PROJECT_ID not set — skipping Vercel push.")
        print(f"Token (first 40 chars): {token_value[:40]}…")
        return

    url = f"https://api.vercel.com/v10/projects/{VERCEL_PROJECT_ID}/env"
    params = {}
    if VERCEL_TEAM_ID:
        params["teamId"] = VERCEL_TEAM_ID

    headers = {"Authorization": f"Bearer {VERCEL_TOKEN}"}

    # Check if env var already exists
    list_resp = requests.get(url, headers=headers, params=params)
    existing_id = None
    if list_resp.ok:
        for env in list_resp.json().get("envs", []):
            if env.get("key") == "ABDM_ACCESS_TOKEN":
                existing_id = env["id"]
                break

    payload = {
        "key": "ABDM_ACCESS_TOKEN",
        "value": token_value,
        "type": "encrypted",
        "target": ["production", "preview", "development"],
    }

    if existing_id:
        resp = requests.patch(f"{url}/{existing_id}", json=payload, headers=headers, params=params)
    else:
        resp = requests.post(url, json=payload, headers=headers, params=params)

    if resp.ok:
        print(f"✅ Token pushed to Vercel (first 40 chars: {token_value[:40]}…)")
    else:
        print(f"❌ Vercel push failed: {resp.status_code} {resp.text}")


def main():
    interval = 14 * 60  # 14 minutes
    print("Starting token refresh loop. Press Ctrl+C to stop.\n")
    while True:
        print(f"[{time.strftime('%H:%M:%S')}] Fetching token via browser…")
        try:
            token = fetch_token_from_browser()
            if token:
                push_to_vercel(token)
            else:
                print("⚠️  No token intercepted — check if the site changed its auth headers.")
        except Exception as e:
            print(f"Error: {e}")

        print(f"Sleeping {interval // 60} min…\n")
        time.sleep(interval)


if __name__ == "__main__":
    main()
