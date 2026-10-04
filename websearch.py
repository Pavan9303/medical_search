from playwright.sync_api import sync_playwright

PORTAL_URL = "https://drugregistrysbx.abdm.gov.in/"

def get_tokens() -> dict:
    """
    Visit the ABDM Drug Registry portal and intercept the apikey + bearer token
    from outgoing XHR/fetch requests made by the frontend JS.
    Returns {"api_key": "...", "bearer": "Bearer ..."}
    """
    tokens = {}

    def on_request(req):
        h = req.headers
        if "apikey" in h and "api_key" not in tokens:
            tokens["api_key"] = h["apikey"]
        if "authorization" in h and "bearer" not in tokens:
            tokens["bearer"] = h["authorization"]  # already has "Bearer " prefix

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("request", on_request)
        page.goto(PORTAL_URL)
        page.wait_for_timeout(8000)
        browser.close()

    return tokens
