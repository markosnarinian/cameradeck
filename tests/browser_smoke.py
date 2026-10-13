"""Explicit read-only smoke against an already-running CameraDeck server.

Run: uv run python tests/browser_smoke.py
CAMERADECK_TEST_URL selects the server. CAMERADECK_PASSWORD optionally authorizes
login. No capture, configuration, upload, power, or deletion requests are sent.
Screenshots are deliberately not taken: the running server may show private media.
"""

import json
import os

from playwright.sync_api import expect, sync_playwright


def run():
    url = os.environ.get("CAMERADECK_TEST_URL", "http://127.0.0.1:8080").rstrip("/")
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("CHROMIUM", "/usr/bin/chromium"),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: errors.append(str(error)))

        # Fail closed if a UI regression attempts a destructive background request.
        def readonly(route):
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                if route.request.url == url + "/api/login" and os.environ.get(
                    "CAMERADECK_PASSWORD"
                ):
                    route.continue_()
                else:
                    errors.append("Unexpected non-read-only request")
                    route.abort()
            else:
                route.continue_()

        page.route("**/*", readonly)
        page.goto(url + "/library")
        session = page.request.get(url + "/api/session").json()
        if not session["authenticated"]:
            expect(page.get_by_role("heading", name="Locked")).to_be_visible()
            if password := os.environ.get("CAMERADECK_PASSWORD"):
                page.get_by_label("Password").fill(password)
                page.get_by_role("button", name="Sign in").click()
                expect(
                    page.get_by_role("heading", name="Library", exact=True)
                ).to_be_visible()
            else:
                assert not errors, errors
                print(
                    json.dumps(
                        {
                            "result": "PASS",
                            "authenticated_checks": "skipped; password not supplied",
                        }
                    )
                )
                browser.close()
                return
        expect(page.get_by_role("heading", name="Library", exact=True)).to_be_visible()
        response = page.request.get(url + "/api/media?per_page=20")
        assert response.ok
        page.get_by_role("link", name="Camera", exact=True).click()
        preview = page.request.get(url + "/api/preview")
        assert preview.status in {200, 503}
        if preview.status == 200:
            assert preview.headers["content-type"].startswith("image/jpeg")
        for width, height in ((1440, 1100), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("link", name="Library", exact=True).click()
            expect(
                page.get_by_role("heading", name="Library", exact=True)
            ).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("link", name="Camera", exact=True).click()
        assert not errors, errors
        print(
            json.dumps(
                {"result": "PASS", "preview_status": preview.status, "read_only": True}
            )
        )
        browser.close()


if __name__ == "__main__":
    run()
