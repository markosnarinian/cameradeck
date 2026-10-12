"""Library browser contracts, using synthetic captures and no camera hardware."""

import os
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from camera import CameraDeck


@pytest.fixture
def library_page(tmp_path):
    server = make_server(
        "127.0.0.1", 0, create_app(CameraDeck(tmp_path)), threaded=True
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=os.environ.get("CHROMIUM", "/usr/bin/chromium"),
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1100})

            def media(route):
                query = parse_qs(urlparse(route.request.url).query)
                number = int(query.get("page", [1])[0])
                route.fulfill(
                    json={
                        "page": number,
                        "pages": 401,
                        "per_page": 40,
                        "total": 16040,
                        "items": [
                            {
                                "id": f"capture-{number}-{index}",
                                "kind": "still",
                                "created": f"2026-10-12T12:00:{index:02}+00:00",
                            }
                            for index in range(40)
                        ],
                    }
                )

            page.route("**/api/media?*", media)
            page.route("**/api/media", media)
            page.route(
                "**/media/*/thumb",
                lambda route: route.fulfill(
                    content_type="image/svg+xml",
                    body='<svg xmlns="http://www.w3.org/2000/svg" width="480" '
                    'height="320"><rect width="480" height="320" fill="#aab9ad"/></svg>',
                ),
            )
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.locator("#library-tab").click()
            expect(page.locator("#page-status")).to_have_text("Page 1 of 401")
            yield page
            browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_library_page_jumps(library_page):
    page = library_page
    previous = page.locator("#previous-pages button")
    following = page.locator("#next-pages button")
    expect(previous).to_have_count(7)
    expect(following).to_have_count(7)
    for button in previous.all():
        expect(button).to_be_disabled()

    current = 1
    for step in (1, 3, 5, 10, 20, 50, 200):
        label = f"Next {step} {'page' if step == 1 else 'pages'}"
        page.get_by_role("button", name=label, exact=True).click()
        current += step
        expect(page.locator("#page-status")).to_have_text(f"Page {current} of 401")
    for step in (1, 3, 5, 10, 20, 50, 200):
        label = f"Previous {step} {'page' if step == 1 else 'pages'}"
        page.get_by_role("button", name=label, exact=True).click()
        current -= step
        expect(page.locator("#page-status")).to_have_text(f"Page {current} of 401")

    page.get_by_role("button", name="Next 3 pages", exact=True).click()
    page.get_by_role("button", name="Previous 200 pages", exact=True).click()
    expect(page.locator("#page-status")).to_have_text("Page 1 of 401")
    for _ in range(2):
        page.get_by_role("button", name="Next 200 pages", exact=True).click()
    for button in following.all():
        expect(button).to_be_disabled()
    page.get_by_role("button", name="Previous 5 pages", exact=True).click()
    page.get_by_role("button", name="Next 200 pages", exact=True).click()
    expect(page.locator("#page-status")).to_have_text("Page 401 of 401")

    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#pagination").scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
