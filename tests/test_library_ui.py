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


def test_library_selection_ranges(library_page):
    page = library_page
    checks = page.locator("#library-grid input[type=checkbox]")

    def selected(indices, total=None):
        expect(page.locator("#selection-count")).to_have_text(
            f"{len(indices) if total is None else total} selected"
        )
        assert (
            checks.evaluate_all(
                "inputs => inputs.flatMap((input, index) => input.checked ? [index] : [])"
            )
            == indices
        )
        expect(page.locator("#library-grid .selected")).to_have_count(len(indices))
        expect(page.locator("#viewer")).not_to_be_visible()

    # With no anchor, modifiers affect only the clicked capture.
    checks.nth(2).click(modifiers=["Shift"])
    selected([2])
    checks.nth(7).click()
    checks.nth(4).click(modifiers=["Shift"])
    selected([2, 4, 5, 6, 7])
    # Shift adds even if its endpoint was already selected.
    checks.nth(6).click(modifiers=["Shift"])
    selected([2, 4, 5, 6, 7])
    checks.nth(4).click(modifiers=["Control"])
    selected([2, 7])
    # Label padding must forward both modifiers to the checkbox as well.
    page.locator("#library-grid .card-select").nth(9).click(
        modifiers=["Control", "Shift"], position={"x": 3, "y": 3}
    )
    selected([2])
    checks.nth(1).click(modifiers=["Control", "Shift"])
    selected([])
    checks.nth(7).click()
    checks.nth(3).click(modifiers=["Shift"])
    selected([3, 4, 5, 6, 7])
    checks.nth(5).click(modifiers=["Control"])
    selected([6, 7])
    checks.nth(3).click(modifiers=["Control"])
    selected([6, 7])
    checks.nth(0).focus()
    checks.nth(0).press("Space")
    selected([0, 6, 7])
    checks.nth(0).press("Space")
    selected([6, 7])

    page.get_by_role("button", name="Next 1 page", exact=True).click()
    expect(page.locator("#page-status")).to_have_text("Page 2 of 401")
    checks.nth(3).click(modifiers=["Control"])
    selected([], total=2)
    checks.nth(3).click(modifiers=["Shift"])
    selected([3], total=3)
    checks.nth(1).click(modifiers=["Shift"])
    selected([1, 2, 3], total=5)
    page.get_by_role("button", name="Previous 1 page", exact=True).click()
    expect(page.locator("#page-status")).to_have_text("Page 1 of 401")
    selected([6, 7], total=5)
    page.locator("#clear-selection").click()
    selected([])
    expect(page.locator("#download-selected")).to_be_disabled()
