"""Real compiled React UI against synthetic Flask media, never camera hardware."""

import base64
import io
import os
import re
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from camera import CameraDeck


def screenshot(page, name, *, full_page=True):
    if directory := os.environ.get("CAMERADECK_SCREENSHOTS"):
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / (name + ".png")), full_page=full_page)


@pytest.fixture
def library_page(tmp_path):
    deck = CameraDeck(tmp_path)
    for index in range(121):
        item = deck._new("still")
        item["file"] = item["id"] + ".jpg"
        image = Image.new("RGB", (160, 100), (30 + index % 100, 90, 110))
        draw = ImageDraw.Draw(image)
        draw.rectangle((15, 40, 145, 80), fill="#78955e")
        draw.ellipse((100, 10, 120, 30), fill="#efc76a")
        draw.text((20, 50), f"Synthetic {index + 1}", fill="white")
        image.save(deck.root / item["file"])
        deck._finish(item, image)
    app = create_app(deck, "synthetic-password", power_manager=Mock())
    assert app.extensions["catalog"].finished.wait(10)
    output = io.BytesIO()
    image.save(output, "JPEG")
    deck.frames.write(output.getvalue())
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = (
                p.webkit.launch()
                if os.environ.get("CAMERADECK_BROWSER") == "webkit"
                else p.chromium.launch(
                    executable_path=os.environ.get("CHROMIUM", "/usr/bin/chromium"),
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            )
            page = browser.new_page(
                viewport={"width": 1440, "height": 1100}, device_scale_factor=2
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/library")
            expect(page.get_by_role("heading", name="Locked")).to_be_visible()
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
            ).strip()
            expect(page.locator("footer code")).to_have_text([revision[:7]])
            expect(page.locator("body")).not_to_contain_text(revision)
            page.get_by_label("Password").fill("synthetic-password")
            page.get_by_role("button", name="Sign in").click()
            expect(page.locator(".media-card")).to_have_count(40)
            expect(page.locator("header")).not_to_contain_text(revision[:7])
            expect(page.locator("footer code")).to_have_text([revision[:7]])
            expect(page.locator("body")).not_to_contain_text(revision)
            yield page
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(10)
        server.server_close()
        app.extensions["catalog"].close()


def test_bottom_status_bar(library_page):
    page = library_page
    base = page.url.split("/library")[0]
    status = page.request.get(base + "/api/status").json()
    status.update(free_bytes=17.25 * 1024**3, total_bytes=64 * 1024**3, low_space=False)
    status["health"] = dict(temperature_c=54.2, current=[], since_boot=[])
    page.route("**/api/status", lambda route: route.fulfill(json=status))
    footer = page.get_by_role("contentinfo", name="System status")
    expect(footer).to_contain_text("17.3 GiB available / 64.0 GiB total")
    expect(footer).to_contain_text("Pi 54.2 °C")
    expect(footer).to_contain_text("No throttling now")
    expect(footer).to_contain_text("Connected · updated")
    screenshot(page, "status-healthy", full_page=False)
    status["health"]["since_boot"] = ["Undervoltage", "Throttled"]
    expect(footer).to_contain_text(
        "No throttling now · Since boot: Undervoltage, Throttled"
    )
    for path in ("/library", "/transfer", "/"):
        page.goto(base + path)
        expect(footer).to_contain_text("17.3 GiB available / 64.0 GiB total")
        for width, height in ((1440, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            assert footer.evaluate("""el => {
                const r = el.getBoundingClientRect();
                const main = document.querySelector('main').getBoundingClientRect();
                return r.bottom <= innerHeight && r.bottom >= innerHeight - 1
                    && main.bottom <= r.top
                    && document.documentElement.scrollWidth <= innerWidth;
            }""")
            screenshot(
                page, f"status-{path.strip('/') or 'camera'}-{width}", full_page=False
            )
            if path == "/transfer":
                button = page.get_by_role("button", name="Delete all media", exact=True)
                button.scroll_into_view_if_needed()
                assert (
                    button.bounding_box()["y"] + button.bounding_box()["height"]
                    <= footer.bounding_box()["y"]
                )
                screenshot(page, f"status-transfer-scrolled-{width}", full_page=False)
    status.update(free_bytes=2.5 * 1024**3, low_space=True)
    expect(footer).to_contain_text("2.5 GiB available / 64.0 GiB total")
    screenshot(page, "status-low-space", full_page=False)
    status["health"].update(
        temperature_c=82.3, current=["Throttled", "Temperature limit"]
    )
    expect(footer).to_contain_text("Pi 82.3 °C")
    expect(footer).to_contain_text("Now: Throttled, Temperature limit")
    screenshot(page, "status-throttled", full_page=False)
    status["health"] = dict(temperature_c=None, current=None, since_boot=None)
    expect(footer).to_contain_text("Pi temperature unavailable")
    expect(footer).to_contain_text("Throttling unavailable")
    expect(footer).not_to_contain_text("No throttling now")
    screenshot(page, "status-unavailable", full_page=False)
    status["health"] = dict(temperature_c=54.2, current=[], since_boot=[])
    expect(footer).to_contain_text("No throttling now")
    # A hung request must become stale before the 15-second request timeout.
    page.clock.install()
    page.reload()
    expect(footer).to_contain_text("Connected · updated")
    page.clock.pause_at(page.evaluate("new Date().toISOString()"))
    pending = []
    page.unroute("**/api/status")
    page.route("**/api/status", lambda route: pending.append(route))
    page.clock.run_for(7000)
    expect(footer).to_contain_text(re.compile(r"Stale · last update [5-9]s ago"))
    screenshot(page, "status-stale", full_page=False)
    assert len(pending) == 1
    pending.pop().abort()
    expect(footer).to_contain_text("Disconnected · last update")
    expect(footer).to_contain_text("2.5 GiB available / 64.0 GiB total · stale")
    screenshot(page, "status-disconnected", full_page=False)
    page.unroute("**/api/status")
    page.route("**/api/status", lambda route: route.fulfill(json=status))
    page.clock.resume()
    expect(footer).to_contain_text("Connected · updated")
    expect(footer).not_to_contain_text(" · stale")
    page.get_by_role("button", name="Lock", exact=True).click()
    expect(footer).to_contain_text("Media disk · unavailable")
    expect(footer).not_to_contain_text("Pi 54.2 °C")
    expect(footer).not_to_contain_text("Connected")
    screenshot(page, "status-locked", full_page=False)


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_header_power_confirmation_and_result(library_page, action):
    page = library_page
    requests, pending = [], []

    def power(route):
        requests.append(route.request.post_data_json)
        pending.append(route)

    page.route("**/api/system/power", power)
    header = page.locator("header")
    expect(header.get_by_role("button", name="Lock", exact=True)).to_be_visible()
    for width, height in ((1440, 1100), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        screenshot(page, f"power-header-{width}", full_page=False)
        header.get_by_role("button", name="Power", exact=True).click()
        page.get_by_role("combobox", name="Action", exact=True).select_option(action)
        dialog = page.get_by_role("dialog")
        submit = dialog.get_by_role(
            "button", name="Shut down Pi" if action == "shutdown" else "Reboot Pi"
        )
        expect(submit).to_be_disabled()
        page.get_by_label(f"Type {action.upper()} to confirm").fill("wrong")
        expect(submit).to_be_disabled()
        page.get_by_label(f"Type {action.upper()} to confirm").fill(action.upper())
        expect(submit).to_be_enabled()
        screenshot(page, f"power-{action}-{width}", full_page=False)
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).not_to_be_visible()
        assert requests == []

    header.get_by_role("button", name="Power", exact=True).click()
    expect(page.get_by_label(f"Type {action.upper()} to confirm")).to_have_value("")
    other = "reboot" if action == "shutdown" else "shutdown"
    page.get_by_label(f"Type {action.upper()} to confirm").fill(action)
    page.get_by_role("combobox", name="Action", exact=True).select_option(other)
    expect(page.get_by_label(f"Type {other.upper()} to confirm")).to_have_value("")
    page.get_by_role("combobox", name="Action", exact=True).select_option(action)
    page.get_by_label(f"Type {action.upper()} to confirm").fill(action.upper())
    submit.click()
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_disabled()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()
    pending.pop(0).fulfill(
        status=409, json={"error": "Stop the rsync transfer before changing Pi power."}
    )
    expect(dialog.get_by_role("alert")).to_contain_text("Stop the rsync transfer")
    expect(submit).to_be_enabled()
    submit.click()
    pending.pop(0).fulfill(status=202, json={"accepted": True, "action": action})
    expect(page.get_by_role("status")).to_have_text(
        "Pi is shutting down…" if action == "shutdown" else "Pi is rebooting…"
    )
    expect(dialog).not_to_be_visible()
    assert requests == [{"action": action, "confirm": action.upper()}] * 2
    screenshot(page, f"power-{action}-accepted", full_page=False)


def test_library_navigation_and_viewer(library_page):
    page = library_page
    top = page.get_by_role("navigation", name="Top pagination")
    bottom = page.get_by_role("navigation", name="Bottom pagination")
    number = top.get_by_role("spinbutton", name="Page number")
    expect(top.get_by_role("button", name="First", exact=True)).to_be_disabled()
    top.get_by_role("button", name="Next", exact=True).click()
    expect(number).to_have_value("2")
    bottom.get_by_role("button", name="Previous", exact=True).click()
    expect(number).to_have_value("1")
    bottom.get_by_role("button", name="Last", exact=True).click()
    expect(number).to_have_value("4")
    expect(page.locator(".media-card")).to_have_count(1)
    expect(top.get_by_role("button", name="Next", exact=True)).to_be_disabled()
    number.fill("2")
    top.get_by_role("button", name="Go", exact=True).click()
    expect(page.locator(".media-card")).to_have_count(40)
    top.get_by_role("button", name="First", exact=True).click()
    expect(number).to_have_value("1")
    page.get_by_role("combobox", name=re.compile("^Per page")).select_option("20")
    expect(page.locator(".media-card")).to_have_count(20)
    page.get_by_role("combobox", name=re.compile("^Thumbnails")).select_option("large")
    expect(page.locator(".media-grid")).to_have_class("media-grid large")
    screenshot(page, "photo-library-desktop")
    page.locator(".thumbnail").first.click()
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.get_by_role("dialog").locator("img")).to_be_visible()
    page.get_by_role("button", name="Load original").click()
    expect(page.get_by_role("button", name="Load original")).to_be_disabled()
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("dialog")).not_to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot(page, "photo-library-mobile")


def test_library_select_all_on_page(library_page):
    page = library_page
    select_all = page.get_by_role("button", name="Select all on page", exact=True)
    checks = page.get_by_role("checkbox")

    expect(select_all).to_be_enabled()
    select_all.click()
    expect(page.locator(".selection-bar > span")).to_contain_text("40 selected")
    expect(checks).to_have_count(40)
    assert checks.evaluate_all("inputs => inputs.every(input => input.checked)")
    expect(select_all).to_be_disabled()
    screenshot(page, "library-select-all")

    page.get_by_role("button", name="Clear", exact=True).click()
    expect(page.locator(".selection-bar > span")).to_contain_text("0 selected")
    assert checks.evaluate_all("inputs => inputs.every(input => !input.checked)")
    expect(select_all).to_be_enabled()


def test_library_mirrored_page_jumps(library_page):
    page = library_page
    base = page.url.split("/library")[0]
    data = page.request.get(base + "/api/media?per_page=40").json()

    def library(route):
        query = parse_qs(urlsplit(route.request.url).query)
        target = min(1501, max(1, int(query.get("page", [1])[0])))
        route.fulfill(
            json={
                **data,
                "page": target,
                "pages": 1501,
                "total": 60040,
                "indexed": 60040,
            }
        )

    page.route("**/api/media?*", library)
    page.get_by_role("button", name="Refresh").click()
    top = page.get_by_role("navigation", name="Top pagination")
    bottom = page.get_by_role("navigation", name="Bottom pagination")
    expect(top.locator("form > span")).to_have_text("of 1501")
    steps = [1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500]
    for bar in (top, bottom):
        assert bar.get_by_role("group", name="Previous pages").get_by_role(
            "button"
        ).all_text_contents() == [f"−{step}" for step in steps] + ["First"]
        assert bar.get_by_role("group", name="Next pages").get_by_role(
            "button"
        ).all_text_contents() == [f"+{step}" for step in steps] + ["Last"]
    grid = page.locator(".media-grid").bounding_box()
    top_box, bottom_box = top.bounding_box(), bottom.bounding_box()
    assert top_box["y"] + top_box["height"] <= grid["y"]
    assert bottom_box["y"] >= grid["y"] + grid["height"]
    if directory := os.environ.get("CAMERADECK_SCREENSHOTS"):
        top.screenshot(path=str(Path(directory) / "pagination-first-desktop.png"))

    def current(target):
        expect(page).to_have_url(re.compile(rf"page={target}(?:&|$)"))
        for bar in (top, bottom):
            expect(bar.get_by_role("spinbutton", name="Page number")).to_have_value(
                str(target)
            )
            expect(bar.get_by_role("button", name="Go", exact=True)).to_be_enabled()

    def go(target):
        top.get_by_role("spinbutton", name="Page number").fill(str(target))
        top.get_by_role("button", name="Go", exact=True).click()
        current(target)

    for step in steps:
        for bar, direction, sign in ((top, "Previous", -1), (bottom, "Next", 1)):
            go(751)
            name = direction if step == 1 else f"{direction} {step} pages"
            bar.get_by_role("button", name=name, exact=True).click()
            current(751 + sign * step)
    go(3)
    top.get_by_role("button", name="Previous 500 pages", exact=True).click()
    current(1)
    for bar in (top, bottom):
        expect(
            bar.get_by_role("group", name="Previous pages").locator("button:enabled")
        ).to_have_count(0)
    go(1499)
    bottom.get_by_role("button", name="Next 500 pages", exact=True).click()
    current(1501)
    for bar in (top, bottom):
        expect(
            bar.get_by_role("group", name="Next pages").locator("button:enabled")
        ).to_have_count(0)
    go(751)
    center = top.locator("form").bounding_box()
    for direction in ("Previous", "Next"):
        boxes = [
            top.get_by_role(
                "button",
                name=direction if step == 1 else f"{direction} {step} pages",
                exact=True,
            ).bounding_box()
            for step in steps
        ]
        assert all(box["y"] == boxes[0]["y"] for box in boxes)
        xs = [box["x"] for box in boxes]
        assert xs == sorted(xs, reverse=direction == "Previous")
        assert (
            max(box["x"] + box["width"] for box in boxes) <= center["x"]
            if direction == "Previous"
            else min(xs) >= center["x"] + center["width"]
        )
    if directory := os.environ.get("CAMERADECK_SCREENSHOTS"):
        top.screenshot(path=str(Path(directory) / "pagination-middle-desktop.png"))
        bottom.screenshot(path=str(Path(directory) / "pagination-bottom-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for step in steps:
        previous = top.get_by_role(
            "button",
            name="Previous" if step == 1 else f"Previous {step} pages",
            exact=True,
        ).bounding_box()
        next_ = top.get_by_role(
            "button", name="Next" if step == 1 else f"Next {step} pages", exact=True
        ).bounding_box()
        assert previous["y"] == next_["y"]
        assert previous["x"] + previous["width"] < next_["x"]
    if directory := os.environ.get("CAMERADECK_SCREENSHOTS"):
        top.screenshot(path=str(Path(directory) / "pagination-mobile-outward.png"))


def test_library_selection_ranges_without_limit(library_page):
    page = library_page
    checks = page.get_by_role("checkbox")
    top = page.get_by_role("navigation", name="Top pagination")

    def selected(indices, total=None):
        # Cross-page range resolution is asynchronous; native state must agree too.
        expect(page.locator(".selection-bar > span")).to_contain_text(
            f"{len(indices) if total is None else total} selected"
        )
        assert (
            checks.evaluate_all(
                "inputs => inputs.flatMap((input, i) => input.checked ? [i] : [])"
            )
            == indices
        )
        for index in indices:
            expect(checks.nth(index)).to_be_checked()
        expect(page.locator(".media-card input:checked")).to_have_count(len(indices))
        expect(page.locator(".media-card.selected")).to_have_count(len(indices))
        expect(page.get_by_role("dialog")).not_to_be_visible()

    checks.nth(2).click(modifiers=["Shift"])
    selected([2])
    page.locator(".select-label span").nth(7).click()
    page.locator(".select-label span").nth(4).click(modifiers=["Shift"])
    selected([2, 4, 5, 6, 7])
    page.locator(".thumbnail").nth(9).click(modifiers=["Shift"])
    selected([2, 4, 5, 6, 7, 8, 9])
    # Control-click on macOS may only deliver pointer-down and a context menu.
    checks.nth(9).hover()
    page.keyboard.down("Control")
    page.keyboard.down("Shift")
    page.mouse.down()
    selected([2, 4, 5, 6])
    checks.nth(9).dispatch_event("contextmenu", {"ctrlKey": True, "shiftKey": True})
    selected([2, 4, 5, 6])
    page.mouse.up()
    page.keyboard.up("Shift")
    page.keyboard.up("Control")
    selected([2, 4, 5, 6])
    page.locator(".thumbnail").nth(6).click(modifiers=["Control", "Shift"])
    selected([2, 4, 5])
    page.locator(".thumbnail").nth(6).click(modifiers=["Shift"])
    selected([2, 4, 5, 6, 7])
    # Refresh preserves the ID anchor; repeated Shift never moves it.
    page.get_by_role("button", name="Refresh").click()
    expect(page.get_by_role("button", name="Refresh")).to_be_enabled()
    checks.nth(9).click(modifiers=["Shift"])
    selected([2, 4, 5, 6, 7, 8, 9])
    page.locator(".thumbnail").nth(4).click(modifiers=["Control"])
    selected([2, 5, 6, 7, 8, 9])
    page.locator(".select-label").nth(9).click(
        modifiers=["Control", "Shift"], position={"x": 3, "y": 3}
    )
    selected([2])
    page.locator(".thumbnail").nth(1).click(modifiers=["Meta", "Shift"])
    selected([])
    checks.nth(0).focus()
    checks.nth(0).press("Space")
    selected([0])
    checks.nth(0).press("Space")
    selected([])
    checks.nth(6).click()
    checks.nth(7).click()
    top.get_by_role("button", name="Next", exact=True).click()
    expect(top.get_by_role("spinbutton", name="Page number")).to_have_value("2")
    expect(checks).to_have_count(40)
    checks.nth(3).click(modifiers=["Control", "Shift"])
    selected([], total=1)
    checks.nth(3).click(modifiers=["Meta", "Shift"])
    selected([], total=1)
    checks.nth(3).click(modifiers=["Shift"])
    selected([0, 1, 2, 3], total=38)
    checks.nth(3).click(modifiers=["Shift"])
    selected([0, 1, 2, 3], total=38)
    checks.nth(1).click()
    checks.nth(3).click(modifiers=["Shift"])
    selected([0, 1, 2, 3], total=38)
    top.get_by_role("button", name="Previous", exact=True).click()
    expect(top.get_by_role("spinbutton", name="Page number")).to_have_value("1")
    expect(checks).to_have_count(40)
    selected(list(range(6, 40)), total=38)
    screenshot(page, "photo-library-selection")
    page.get_by_role("button", name="Clear", exact=True).click()
    selected([])
    checks.nth(5).click(modifiers=["Control", "Shift"])
    selected([])
    checks.nth(5).click(modifiers=["Meta", "Shift"])
    selected([])
    checks.nth(5).click(modifiers=["Shift"])
    selected([5])
    checks.nth(5).click(modifiers=["Shift"])
    selected([5])
    checks.nth(5).click()
    selected([])
    checks.nth(5).click()
    selected([5])
    page.get_by_role("combobox", name="Kind", exact=True).select_option("still")
    expect(page).to_have_url(re.compile("kind=still"))
    expect(page.get_by_role("button", name="Refresh")).to_be_enabled()
    checks.nth(5).click(modifiers=["Shift"])
    selected([5])
    # With no previous anchor, the first Shift-click starts the new range.
    checks.nth(8).click(modifiers=["Control", "Shift"])
    selected([])
    checks.nth(5).click(modifiers=["Meta", "Shift"])
    selected([])
    expect(page.get_by_role("button", name="ZIP", exact=True)).to_be_disabled()
    page.get_by_role("combobox", name=re.compile("^Per page")).select_option("100")
    expect(checks).to_have_count(100)
    checks.first.click()
    checks.last.click(modifiers=["Shift"])
    selected(list(range(100)))
    top.get_by_role("button", name="Next", exact=True).click()
    expect(checks).to_have_count(21)
    checks.first.click()
    selected([0], total=101)
    checks.last.click(modifiers=["Shift"])
    selected(list(range(21)), total=121)


def test_library_ranges_across_skipped_pages(library_page):
    page = library_page
    checks = page.get_by_role("checkbox")
    top = page.get_by_role("navigation", name="Top pagination")

    def go(number):
        top.get_by_role("spinbutton", name="Page number").fill(str(number))
        top.get_by_role("button", name="Go", exact=True).click()
        expect(page.get_by_role("button", name="Refresh")).to_be_enabled()
        expect(top.get_by_role("spinbutton", name="Page number")).to_have_value(
            str(number)
        )

    def selected(indices, total):
        expect(page.locator(".selection-bar > span")).to_contain_text(
            f"{total} selected"
        )
        expect(page.locator(".media-card.selected")).to_have_count(len(indices))
        assert (
            checks.evaluate_all(
                "inputs => inputs.flatMap((input, i) => input.checked ? [i] : [])"
            )
            == indices
        )

    checks.nth(35).click()
    go(3)
    checks.nth(4).click(modifiers=["Shift"])
    selected(list(range(5)), 50)
    go(2)
    selected(list(range(40)), 50)
    checks.nth(10).click(modifiers=["Control", "Shift"])
    selected(list(range(11, 40)), 34)
    go(3)
    checks.nth(4).click(modifiers=["Meta", "Shift"])
    selected([], 0)
    checks.nth(4).click(modifiers=["Shift"])
    selected(list(range(5)), 50)
    page.get_by_role("combobox", name=re.compile("^Per page")).select_option("20")
    expect(checks).to_have_count(20)
    go(1)
    checks.first.click(modifiers=["Shift"])
    selected(list(range(20)), 85)
    # Establish an anchor at 0, then remove and add a range beyond the old limit.
    checks.first.click()
    go(7)
    expect(checks).to_have_count(1)
    checks.first.click(modifiers=["Control", "Shift"])
    selected([], 0)
    checks.first.click(modifiers=["Shift"])
    selected([0], 121)


def test_library_first_shift_click_anchors_across_pages(library_page):
    page = library_page
    checks = page.get_by_role("checkbox")
    top = page.get_by_role("navigation", name="Top pagination")
    checks.nth(35).click(modifiers=["Shift"])
    top.get_by_role("button", name="Next 2 pages", exact=True).click()
    expect(page.get_by_role("button", name="Refresh")).to_be_enabled()
    pending = []
    page.route("**/api/media/range", lambda route: pending.append(route))
    checks.nth(4).click(modifiers=["Shift"])
    expect(
        page.get_by_role("progressbar", name="Resolving selection")
    ).not_to_have_attribute("value")
    expect(page.locator(".operation-progress small")).to_contain_text("ETA unavailable")
    expect(page.get_by_role("button", name="Clear", exact=True)).to_be_disabled()
    screenshot(page, "library-selection-progress", full_page=False)
    pending.pop(0).continue_()
    page.unroute("**/api/media/range")
    expect(page.locator(".selection-bar > span")).to_contain_text("50 selected")
    expect(page.locator(".media-card input:checked")).to_have_count(5)
    top.get_by_role("button", name="Previous", exact=True).click()
    expect(page.get_by_role("button", name="Refresh")).to_be_enabled()
    expect(page.locator(".media-card input:checked")).to_have_count(40)


def test_library_delete_progress_and_partial_failure(library_page):
    page = library_page
    checks = page.get_by_role("checkbox")
    checks.first.click()
    top = page.get_by_role("navigation", name="Top pagination")
    top.get_by_role("button", name="Last", exact=True).click()
    expect(checks).to_have_count(1)
    checks.first.click(modifiers=["Shift"])
    expect(page.locator(".selection-bar > span")).to_contain_text("121 selected")
    pending = []
    page.route("**/api/media/delete", lambda route: pending.append(route))
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now()"))
    page.get_by_role("button", name="Delete", exact=True).click()
    dialog = page.get_by_role("dialog")
    close = dialog.get_by_role("button", name="Close", exact=True)
    expect(close).to_be_enabled()
    screenshot(page, "library-delete-review", full_page=False)

    def cancel_confirmation(prompt):
        assert prompt.type == "confirm"
        assert "Permanently delete 121 selected captures" in prompt.message
        assert "including 120 on other pages / filters" in prompt.message
        assert "This cannot be undone" in prompt.message
        prompt.dismiss()

    page.once("dialog", cancel_confirmation)
    page.get_by_role("button", name="Confirm deletion").click()
    expect(close).to_be_enabled()
    expect(page.get_by_role("progressbar")).to_have_count(0)
    assert not pending
    base = page.url.split("/library")[0]
    assert page.request.get(base + "/api/media").json()["total"] == 121
    close.click()
    expect(dialog).not_to_be_visible()
    page.get_by_role("button", name="Delete", exact=True).click()
    page.once("dialog", lambda prompt: prompt.accept())
    page.get_by_role("button", name="Confirm deletion").click()
    progress = page.get_by_role("progressbar", name="Deleting captures")
    expect(progress).to_have_attribute("value", "0")
    expect(progress).to_have_attribute("max", "121")
    expect(close).to_be_disabled()
    close.click(force=True)
    page.keyboard.press("Escape")
    page.mouse.click(5, 5)
    expect(dialog).to_be_visible()
    expect(page.locator(".operation-progress small")).to_contain_text("ETA: estimating")
    page.clock.run_for(10000)
    first = pending.pop(0)
    first.fulfill(response=first.fetch())
    expect(progress).to_have_attribute("value", "100")
    expect(page.locator(".operation-progress small")).to_contain_text(
        "ETA: 3s remaining (estimated)"
    )
    screenshot(page, "library-delete-progress", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    screenshot(page, "library-delete-progress-mobile", full_page=False)
    page.set_viewport_size({"width": 1440, "height": 1100})
    pending.pop(0).fulfill(status=503, json={"error": "Synthetic delete failure"})
    page.clock.resume()
    expect(page.get_by_role("dialog").get_by_role("status")).to_contain_text(
        "100 deleted; 0 already missing.\nSynthetic delete failure"
    )
    expect(close).to_be_enabled()
    expect(page.locator(".selection-bar > span")).to_contain_text("21 selected")
    page.keyboard.press("Escape")
    expect(checks).to_have_count(21)
    expect(page.locator(".media-card input:checked")).to_have_count(21)
    page.unroute("**/api/media/delete")
    page.get_by_role("button", name="Delete", exact=True).click()
    page.once("dialog", lambda prompt: prompt.accept())
    page.get_by_role("button", name="Confirm deletion").click()
    expect(page.get_by_role("dialog").get_by_role("status")).to_contain_text(
        "21 deleted"
    )
    expect(close).to_be_enabled()
    expect(page.locator(".selection-bar > span")).to_contain_text("0 selected")
    page.keyboard.press("Escape")
    expect(page.locator(".media-card")).to_have_count(0)


def test_library_zip_preparation_and_byte_progress(library_page, tmp_path):
    page = library_page
    base = page.url.split("/library")[0]
    checks = page.get_by_role("checkbox")
    checks.first.click()
    page.get_by_role("navigation", name="Top pagination").get_by_role(
        "button", name="Last", exact=True
    ).click()
    expect(checks).to_have_count(1)
    checks.first.click(modifiers=["Shift"])
    expect(page.locator(".selection-bar > span")).to_contain_text("121 selected")
    screenshot(page, "library-unlimited-selection", full_page=False)
    ids = []
    for number in (1, 2):
        ids.extend(
            item["id"]
            for item in page.request.get(
                base + f"/api/media?per_page=100&page={number}"
            ).json()["items"]
        )
    archive = page.request.post(base + "/api/media/download", data={"ids": ids}).body()
    # Deliver the real archive in controlled chunks, exercising the browser's
    # ReadableStream without depending on loopback transfer speed.
    page.evaluate(
        """encoded => {
      const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
      const fetch = window.fetch;
      window.fetch = (url, options) => url !== '/api/media/download'
        ? fetch(url, options)
        : new Promise(resolve => {
          window.beginZIP = () => resolve(new Response(new ReadableStream({
            start(controller) {
              const middle = Math.floor(bytes.length / 2);
              controller.enqueue(bytes.slice(0, middle));
              window.finishZIP = () => {
                controller.enqueue(bytes.slice(middle));
                controller.close();
              };
            }
          }), { headers: { 'Content-Length': String(bytes.length), 'Content-Type': 'application/zip' } }));
        });
    }""",
        base64.b64encode(archive).decode(),
    )
    page.get_by_role("button", name="ZIP", exact=True).click()
    expect(page.get_by_role("progressbar", name="Preparing ZIP")).not_to_have_attribute(
        "value"
    )
    expect(page.locator(".operation-progress small")).to_contain_text("ETA unavailable")
    screenshot(page, "library-zip-preparation", full_page=False)
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now()"))
    page.clock.run_for(60000)
    page.evaluate("window.beginZIP()")
    progress = page.get_by_role("progressbar", name="Downloading ZIP")
    expect(progress).to_have_attribute("value", str(len(archive) // 2))
    expect(progress).to_have_attribute("max", str(len(archive)))
    expect(page.locator(".operation-progress small")).to_contain_text("ETA: estimating")
    page.clock.run_for(10000)
    expect(page.locator(".operation-progress small")).to_contain_text(
        f"ETA: {10 if len(archive) % 2 == 0 else 11}s remaining (estimated)"
    )
    screenshot(page, "library-zip-transfer", full_page=False)
    page.clock.resume()
    with page.expect_download() as downloaded:
        page.evaluate("window.finishZIP()")
    output = tmp_path / "download.zip"
    downloaded.value.save_as(output)
    assert output.read_bytes() == archive
    expect(page.get_by_role("progressbar")).to_have_count(0)


@pytest.mark.parametrize("target", ["checkbox", "date", "thumbnail"])
def test_library_modifier_changes_during_click(library_page, target):
    page = library_page
    control = {
        "checkbox": page.get_by_role("checkbox"),
        "date": page.locator(".select-label span"),
        "thumbnail": page.locator(".thumbnail"),
    }[target]
    control.nth(2).hover()
    page.keyboard.down("Control")
    page.mouse.down()
    page.keyboard.up("Control")
    page.mouse.up()
    expect(page.locator(".selection-bar > span")).to_contain_text("1 selected")
    expect(page.get_by_role("checkbox").nth(2)).to_be_checked()
    expect(page.get_by_role("dialog")).not_to_be_visible()
    # Conversely, Control may be pressed after pointer-down but before the click.
    control.nth(5).hover()
    page.mouse.down()
    page.keyboard.down("Control")
    page.mouse.up()
    page.keyboard.up("Control")
    expect(page.locator(".selection-bar > span")).to_contain_text("2 selected")
    expect(page.get_by_role("checkbox").nth(5)).to_be_checked()
    expect(page.get_by_role("dialog")).not_to_be_visible()


def test_library_session_reload_recovery_and_settings(library_page):
    page = library_page
    page.reload()
    expect(page.locator(".media-card")).to_have_count(40)
    expect(page.get_by_role("button", name="Upload", exact=True)).to_have_count(0)
    # A failed real library request is recoverable without losing the session.
    page.route(
        "**/api/media?*",
        lambda route: route.fulfill(
            status=503, json={"error": "Synthetic index unavailable"}
        ),
    )
    page.get_by_role("button", name="Refresh").click()
    # The low-space warning is also an alert when the host has under 5 GB free.
    expect(page.locator(".empty[role=alert]")).to_contain_text(
        "Synthetic index unavailable"
    )
    screenshot(page, "photo-library-error")
    page.unroute("**/api/media?*")
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(page.locator(".media-card")).to_have_count(40)
    page.get_by_role("button", name="Lock", exact=True).click()
    expect(page.get_by_role("heading", name="Locked")).to_be_visible()
    page.get_by_label("Password").fill("wrong-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_have_text("Incorrect password.")
    page.get_by_label("Password").fill("synthetic-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator(".media-card")).to_have_count(40)


def test_transfer_delete_all_confirmation_and_result(library_page):
    page = library_page
    page.get_by_role("link", name="Transfer", exact=True).click()
    base = page.url.split("/transfer")[0]
    open_delete = page.get_by_role("button", name="Delete all media", exact=True)
    open_delete.click()
    dialog = page.get_by_role("dialog")
    confirm = dialog.get_by_role(
        "button", name="Permanently delete all media", exact=True
    )
    expect(confirm).to_be_disabled()
    page.get_by_label("Type DELETE ALL to confirm").fill("DELETE")
    expect(confirm).to_be_disabled()
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    assert page.request.get(base + "/api/media").json()["total"] == 121
    open_delete.click()
    expect(page.get_by_label("Type DELETE ALL to confirm")).to_have_value("")
    screenshot(page, "delete-all-confirmation-desktop", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_label("Type DELETE ALL to confirm").fill("delete all")
    screenshot(page, "delete-all-confirmation-narrow", full_page=False)
    pending = []
    page.route("**/api/media/delete-all", lambda route: pending.append(route))
    progress = {"active": True, "completed": 0, "total": None, "eta_seconds": None}
    page.route(
        "**/api/media/delete-all/progress", lambda route: route.fulfill(json=progress)
    )
    confirm.click()
    expect(dialog.get_by_role("button", name="Deleting…", exact=True)).to_be_disabled()
    expect(
        dialog.get_by_role("button", name="Cancel deletion", exact=True)
    ).to_be_enabled()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("status")).to_contain_text("Deleting all Library media")
    bar = dialog.get_by_role("progressbar", name="Deleting all Library media")
    expect(bar).not_to_have_attribute("value")
    expect(dialog.get_by_role("status")).to_contain_text("ETA: estimating…")
    screenshot(page, "delete-all-estimating-narrow", full_page=False)
    progress.update(completed=37, total=121, eta_seconds=83.2)
    expect(bar).to_have_attribute("value", "37")
    expect(bar).to_have_attribute("max", "121")
    expect(dialog.get_by_role("status")).to_contain_text("37 / 121 captures deleted")
    expect(dialog.get_by_role("status")).to_contain_text("ETA: 1m 24s remaining")
    screenshot(page, "delete-all-progress-narrow", full_page=False)
    page.set_viewport_size({"width": 1440, "height": 1100})
    screenshot(page, "delete-all-progress-desktop", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # A failed poll must not cancel the destructive request or invent progress.
    page.unroute("**/api/media/delete-all/progress")
    page.route("**/api/media/delete-all/progress", lambda route: route.abort())
    expect(bar).not_to_have_attribute("value")
    expect(
        dialog.get_by_role("button", name="Cancel deletion", exact=True)
    ).to_be_enabled()
    page.unroute("**/api/media/delete-all/progress")
    assert pending[0].request.post_data_json == {"confirm": "delete all"}
    pending.pop().fulfill(
        status=409, json={"error": "CameraDeck must be idle before deleting all media."}
    )
    expect(dialog.get_by_role("alert")).to_contain_text("must be idle")
    assert page.request.get(base + "/api/media").json()["total"] == 121
    page.unroute("**/api/media/delete-all")
    confirm.click()
    expect(dialog).not_to_be_visible()
    expect(
        page.get_by_role("region", name="Free up space").get_by_role("status")
    ).to_contain_text("Deleted 121 captures from the Pi.")
    assert page.request.get(base + "/api/media").json()["total"] == 0
    screenshot(page, "delete-all-success-narrow", full_page=False)
    page.get_by_role("link", name="Library", exact=True).click()
    expect(page.locator(".media-card")).to_have_count(0)


def test_transfer_delete_all_cancellation(library_page):
    page = library_page
    page.get_by_role("link", name="Transfer", exact=True).click()
    pending, cancellations = [], []
    page.route("**/api/media/delete-all", lambda route: pending.append(route))
    page.route(
        "**/api/media/delete-all/cancel", lambda route: cancellations.append(route)
    )
    page.route(
        "**/api/media/delete-all/progress",
        lambda route: route.fulfill(
            json={"active": True, "completed": 37, "total": 121, "eta_seconds": 83.2}
        ),
    )
    page.get_by_role("button", name="Delete all media", exact=True).click()
    page.get_by_label("Type DELETE ALL to confirm").fill("delete all")
    dialog = page.get_by_role("dialog")
    dialog.get_by_role(
        "button", name="Permanently delete all media", exact=True
    ).click()
    cancel = dialog.get_by_role("button", name="Cancel deletion", exact=True)
    expect(cancel).to_be_enabled()
    cancel.click()
    expect(
        dialog.get_by_role("button", name="Cancelling…", exact=True)
    ).to_be_disabled()
    expect(dialog.get_by_role("status")).to_contain_text(
        "Stopping after the current capture"
    )
    cancellations.pop().fulfill(
        status=503, json={"error": "Cancellation failed. Try again."}
    )
    expect(dialog.get_by_role("alert")).to_contain_text("Cancellation failed")
    expect(cancel).to_be_enabled()
    cancel.click()
    expect(
        dialog.get_by_role("button", name="Cancelling…", exact=True)
    ).to_be_disabled()
    cancellations.pop().fulfill(json={"ok": True})
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_disabled()
    expect(dialog.get_by_role("status")).to_contain_text("cannot be restored")
    screenshot(page, "delete-all-cancelling-desktop", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot(page, "delete-all-cancelling-narrow", full_page=False)
    pending.pop().fulfill(
        json={
            "deleted": [str(index) for index in range(38)],
            "missing": [],
            "cancelled": True,
            "remaining": 83,
        }
    )
    expect(dialog).not_to_be_visible()
    feedback = page.get_by_role("region", name="Free up space").get_by_role("status")
    expect(feedback).to_contain_text("Deletion cancelled. Deleted 38 captures")
    expect(feedback).to_contain_text("83 captures left untouched")
    expect(feedback).to_contain_text("Already deleted captures cannot be restored")
    feedback.scroll_into_view_if_needed()
    screenshot(page, "delete-all-cancelled-narrow", full_page=False)
    page.get_by_role("button", name="Delete all media", exact=True).click()
    expect(page.get_by_label("Type DELETE ALL to confirm")).to_have_value("")
    expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_enabled()


@pytest.mark.skip(reason="Superseded by the rsync-only transfer page")
def test_transfer_rsync_and_optional_upload(library_page):
    page = library_page
    page.get_by_role("link", name="Transfer", exact=True).click()
    expect(page.get_by_role("heading", name="Transfer", exact=True)).to_be_visible()
    expect(page.get_by_role("checkbox")).to_have_count(0)
    expect(page.get_by_role("button", name="Upload", exact=True)).to_have_count(0)
    command = page.get_by_label("rsync command").inner_text()
    assert command.startswith(
        "rsync --archive --info=progress2,stats2 --human-readable --whole-file"
    )
    assert "--ignore-existing" not in command
    for pattern in ("*.preview.jpg", "*.thumb.jpg", ".*", "*.tmp", "*.pending.*"):
        assert f"--exclude='{pattern}'" in command
    assert command.endswith(
        "  /home/pi/cameradeck/media/ \\\n  faros@192.168.1.10:/mnt/wd2tb/faros"
    )
    expect(page.get_by_label("Destination", exact=True)).to_have_value(
        "faros@192.168.1.10:/mnt/wd2tb/faros"
    )
    assert "\\\n" in command and "\\n" not in command
    # The Pi serves plain HTTP, so exercise the non-Clipboard-API fallback.
    page.evaluate("""() => {
            Object.defineProperty(navigator, "clipboard", { value: undefined });
            window.copied = [];
            document.execCommand = (name) => {
                window.copied.push([name, document.activeElement.value]);
                return true;
            };
        }""")
    page.get_by_role("button", name="Copy", exact=True).click()
    expect(page.get_by_role("button", name="Copied", exact=True)).to_be_visible()
    assert page.evaluate("window.copied") == [["copy", command]]
    page.reload()
    expect(page.get_by_role("heading", name="Transfer", exact=True)).to_be_visible()
    screenshot(page, "transfer-desktop")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot(page, "transfer-mobile")
    page.locator("summary").filter(has_text="S3 upload").click()
    expect(page.locator(".media-card")).to_have_count(40)
    expect(page.get_by_role("button", name="Delete", exact=True)).to_have_count(0)
    checks = page.get_by_role("checkbox")
    base = page.url.split("/transfer")[0]
    ids = [
        item["id"]
        for item in page.request.get(base + "/api/media?per_page=40").json()["items"][
            :3
        ]
    ]
    payloads, pending = [], []

    def upload(route):
        payloads.append(route.request.post_data_json)
        pending.append(route)

    page.route("**/api/media/upload", upload)
    checks.first.click()
    checks.nth(2).click(modifiers=["Shift"])
    expect(page.locator(".selection-bar > span")).to_contain_text("3 selected")
    assert checks.evaluate_all(
        "inputs => inputs.slice(0, 3).map(input => input.checked)"
    ) == [True, True, True]
    screenshot(page, "transfer-s3-mobile", full_page=False)
    page.get_by_role("button", name="Upload", exact=True).click()
    expect(page.get_by_label("S3 endpoint")).to_have_value("http://192.168.1.10:3900")
    page.get_by_label("Bucket", exact=True).fill("test-bucket")
    page.get_by_label("Prefix", exact=True).fill("photos/")
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now()"))
    page.get_by_role("button", name="Confirm upload").click()
    progress = page.get_by_role("progressbar", name="Uploading captures")
    expect(progress).to_have_attribute("value", "0")
    expect(progress).to_have_attribute("max", "3")
    expect(page.locator(".operation-progress small")).to_contain_text("ETA: estimating")
    page.clock.run_for(10000)
    pending.pop(0).fulfill(json={"results": [{"id": ids[0], "status": "uploaded"}]})
    expect(progress).to_have_attribute("value", "1")
    expect(page.locator(".operation-progress small")).to_contain_text(
        "ETA: 20s remaining (estimated)"
    )
    screenshot(page, "library-upload-progress-mobile", full_page=False)
    page.clock.run_for(10000)
    pending.pop(0).fulfill(json={"results": [{"id": ids[1], "status": "uncertain"}]})
    expect(progress).to_have_attribute("value", "2")
    expect(page.locator(".operation-progress small")).to_contain_text(
        "ETA: 10s remaining (estimated)"
    )
    pending.pop(0).fulfill(json={"results": [{"id": ids[2], "status": "error"}]})
    page.clock.resume()
    expect(page.get_by_role("dialog").get_by_role("status")).to_contain_text(
        "1 uploaded / already present; 2 need attention."
    )
    assert payloads == [
        {
            "ids": [ident],
            "endpoint": "http://192.168.1.10:3900",
            "bucket": "test-bucket",
            "prefix": "photos/",
        }
        for ident in ids
    ]
    page.keyboard.press("Escape")
    expect(page.locator(".selection-bar > span")).to_contain_text("2 selected")
    page.get_by_role("link", name="Library", exact=True).click()
    expect(page.get_by_role("button", name="Upload", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="ZIP", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Delete", exact=True)).to_be_visible()
    screenshot(page, "library-without-upload-mobile", full_page=False)


@pytest.mark.parametrize("modern", [True, False])
def test_independent_exposure_modes(library_page, modern):
    page = library_page
    base = page.url.split("/library")[0]
    status = page.request.get(base + "/api/status").json()
    status.update(
        ready=True,
        metadata_age=0,
        metadata={"ExposureTime": 2300, "AnalogueGain": 2.5},
        applied={"AeEnable": True},
        controls={
            "AeEnable": dict(type="Bool", min=False, max=True, default=True),
            "ExposureTime": dict(type="Integer32", min=100, max=10000, default=1000),
            "AnalogueGain": dict(type="Float", min=1, max=8, default=1),
        },
    )
    if modern:
        for mode in ("ExposureTimeMode", "AnalogueGainMode"):
            status["controls"][mode] = dict(
                type="Integer32",
                min=0,
                max=1,
                default=0,
                options={"Auto": 0, "Manual": 1},
            )
            status["applied"][mode] = 0
    payloads = []
    reject = False

    def controls(route):
        payload = route.request.post_data_json
        payloads.append(payload)
        if reject:
            route.fulfill(status=400, json={"error": "Control rejected"})
            return
        status["applied"].update(payload)
        if modern:
            for name in ("ExposureTime", "AnalogueGain"):
                if status["applied"][name + "Mode"] == 0:
                    status["applied"].pop(name, None)
        else:
            status["applied"].pop("AeEnable", None)
        route.fulfill(json={"ok": True})

    page.route("**/api/status", lambda route: route.fulfill(json=status))
    page.route("**/api/controls", controls)
    page.goto(base + "/")
    page.get_by_role("button", name="Settings", exact=True).click()
    exposure = page.get_by_label("Exposure time mode", exact=True)
    gain = page.get_by_label("Gain mode", exact=True)
    time_value = page.get_by_label("Requested exposure · µs")
    gain_value = page.get_by_label("Requested gain · ×")
    expect(time_value).to_be_disabled()
    expect(gain_value).to_be_disabled()
    screenshot(page, "exposure-auto" if modern else "exposure-legacy-auto")

    exposure.select_option("manual")
    expect(time_value).to_be_enabled()
    expect(time_value).to_have_value("2300")
    expect(gain_value).to_be_disabled()
    assert payloads[-1] == (
        {"ExposureTimeMode": 1, "ExposureTime": 2300}
        if modern
        else {"ExposureTime": 2300, "AnalogueGain": 0}
    )
    time_value.fill("4100")
    time_value.press("Tab")
    expect(exposure).to_be_enabled()
    assert payloads[-1] == {"ExposureTime": 4100}
    screenshot(page, "exposure-shutter-priority") if modern else None

    gain.select_option("manual")
    expect(gain_value).to_be_enabled()
    expect(gain_value).to_have_value("2.5")
    expect(time_value).to_have_value("4100")
    screenshot(page, "exposure-manual") if modern else None
    exposure.select_option("auto")
    expect(time_value).to_be_disabled()
    expect(gain_value).to_be_enabled()
    expect(gain_value).to_have_value("2.5")
    assert payloads[-1] == (
        {"ExposureTimeMode": 0} if modern else {"ExposureTime": 0, "AnalogueGain": 2.5}
    )
    screenshot(page, "exposure-gain-priority") if modern else None
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Settings", exact=True).click()
    expect(exposure).to_have_value("auto")
    expect(gain).to_have_value("manual")
    expect(gain_value).to_have_value("2.5")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot(page, "exposure-gain-priority-narrow") if modern else None

    reject = True
    exposure.select_option("manual")
    # The preview also renders a status once the fixture's single frame is stale.
    expect(page.locator(".notice[role=status]")).to_contain_text("Control rejected")
    expect(exposure).to_have_value("auto")
    expect(time_value).to_be_disabled()
    reject = False
    status["metadata_age"] = 10
    expect(page.locator(".measured")).to_contain_text("Unavailable / stale")
    exposure.select_option("manual")
    expect(time_value).to_have_value("1000")
    expect(gain_value).to_have_value("2.5")
    gain.select_option("auto")
    expect(gain_value).to_be_disabled()
    expect(time_value).to_be_enabled()
    exposure.select_option("auto")
    expect(time_value).to_be_disabled()
    expect(gain_value).to_be_disabled()


def test_camera_preview_and_requested_vs_measured(library_page):
    page = library_page
    base = page.url.split("/library")[0]
    status = page.request.get(base + "/api/status").json()
    status.update(
        ready=True,
        low_space=False,
        metadata_age=0,
        metadata={
            "ExposureTime": 2300,
            "AnalogueGain": 2,
            "FrameDuration": 40000,
            "LensPosition": 1.75,
            "ColourTemperature": 4870,
            "Lux": 123.4,
        },
        applied={"AeEnable": False, "ExposureTime": 1000, "AnalogueGain": 4},
        controls={
            "AeEnable": {
                "type": "Bool",
                "min": False,
                "max": True,
                "default": True,
                "size": 0,
                "options": {},
            },
            "ExposureTime": {
                "type": "Integer32",
                "min": 100,
                "max": 10000,
                "default": 1000,
                "size": 0,
                "options": {},
            },
            "AnalogueGain": {
                "type": "Float",
                "min": 1,
                "max": 8,
                "default": 1,
                "size": 0,
                "options": {},
            },
        },
    )
    item = page.request.get(base + "/api/media?per_page=1").json()["items"][0]
    jpeg = page.request.get(base + f'/media/{item["id"]}/original').body()
    page.route("**/api/status", lambda route: route.fulfill(json=status))
    page.route(
        "**/api/preview*",
        lambda route: route.fulfill(content_type="image/jpeg", body=jpeg),
    )
    # Slow snapshot completion exposes overlapping pollers without a real camera.
    page.add_init_script("""const originalFetch = window.fetch;
      window.previewStats = {active: 0, max: 0, calls: 0};
      window.fetch = async (...args) => {
        if (!String(args[0]).startsWith('/api/preview')) return originalFetch(...args);
        const stats = window.previewStats;
        stats.calls++; stats.active++; stats.max = Math.max(stats.max, stats.active);
        try { const result = await originalFetch(...args);
          await new Promise(resolve => setTimeout(resolve, 400)); return result;
        } finally { stats.active--; }
      };""")
    page.goto(base + "/")
    expect(page.get_by_role("heading", name="Camera", exact=True)).to_have_count(1)
    # Observe several snapshot intervals; evaluate avoids CSP-blocked predicate eval.
    page.wait_for_timeout(2500)
    assert page.evaluate("window.previewStats.calls") >= 3
    assert page.evaluate("window.previewStats.max") == 1
    readings = page.get_by_role("region", name="Measured camera statistics")
    expect(readings.locator("dd")).to_have_text(
        ["2.30 ms", "2.00×", "25.0 fps", "1.75 dpt", "4870 K", "123.4 lx"]
    )
    status["metadata"]["Lux"] = 98.7
    expect(readings.locator("dd").last).to_have_text("98.7 lx")
    screenshot(page, "photo-camera-desktop")
    payloads = []

    def start(route):
        payloads.append(route.request.post_data_json)
        status["sequence"] = {
            "active": True,
            "completed": 25,
            "total": 0,
            "rate": 4.9,
            "skipped": 1,
            "settings": {"rate": 5},
        }
        route.fulfill(json={"ok": True})

    def stop(route):
        status["sequence"] = None
        route.fulfill(json={"ok": True})

    page.route("**/api/sequence/start", start)
    page.route("**/api/sequence/stop", stop)
    expect(page.get_by_role("spinbutton", name="Photos per second")).to_have_value("5")
    page.get_by_role("button", name="Start", exact=True).click()
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
    assert payloads == [{"mode": "burst", "settings": {"rate": 5, "count": 0}}]
    expect(page.locator(".sequence")).to_contain_text("25 photos")
    # Include reduced CSS viewports for Android display scaling/browser chrome,
    # and iPhone landscape with Safari's toolbars still expanded.
    viewports = (
        (1440, 800),
        (1280, 720),
        (1920, 1080),
        (1080, 1920),
        (960, 540),
        (600, 900),
        (390, 664),
        (844, 284),
        (750, 284),
    )
    for capturing in (False, True):
        status["sequence"]["active"] = capturing
        expect(page.locator(".sequence")).to_contain_text(
            "Capturing" if capturing else "Stopped"
        )
        for warning in (False, True):
            status.update(low_space=warning, free_bytes=2.7 * 1024**3)
            if warning:
                expect(page.locator(".warning")).to_have_text(
                    "Low disk space: 2.7 GB free. Capture stops below 256 MB — transfer and remove captures soon."
                )
            else:
                expect(page.locator(".warning")).to_have_count(0)
            for width, height in viewports:
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate(
                    "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
                )
                assert page.evaluate("""() => {
          const doc = document.documentElement;
          const within = el => {
            const r = el.getBoundingClientRect();
            return r.left >= 0 && r.top >= 0 && r.right <= innerWidth + 1
              && r.bottom <= innerHeight + 1 && r.width > 0 && r.height > 0;
          };
          const preview = document.querySelector('.preview').getBoundingClientRect();
          const readings = document.querySelector('.camera-readings').getBoundingClientRect();
          const controls = [...document.querySelectorAll('main button, main input')];
          const noOverlap = controls.every((a, i) => controls.slice(i + 1).every(b => {
            const x = a.getBoundingClientRect(), y = b.getBoundingClientRect();
            return Math.min(x.right, y.right) <= Math.max(x.left, y.left) + 1
              || Math.min(x.bottom, y.bottom) <= Math.max(x.top, y.top) + 1;
          }));
          return doc.scrollWidth <= innerWidth && doc.scrollHeight <= innerHeight
            && readings.top >= preview.bottom && noOverlap
            && [...document.querySelectorAll('dt, dd')].every(el => el.scrollWidth <= el.clientWidth + 1)
            && [...document.querySelectorAll('header a, header button, main button, main input, dt, dd, .preview')].every(within);
        }"""), (capturing, warning, width, height)
    page.set_viewport_size({"width": 1440, "height": 1100})
    screenshot(page, "photo-camera-capturing")
    page.get_by_role("link", name="Library", exact=True).click()
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
    page.get_by_role("button", name="Stop", exact=True).click()
    page.get_by_role("link", name="Camera", exact=True).click()
    expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Settings", exact=True).click()
    expect(page.get_by_label("Requested exposure · µs")).to_have_value("1000")
    expect(page.get_by_label("Requested gain · ×")).to_have_value("4")
    expect(page.locator(".measured")).to_have_text(
        "Measured exposure: 2300 µs · 2× gain"
    )
    screenshot(page, "photo-camera-settings")
    status["metadata_age"] = 10
    expect(page.locator(".measured")).to_contain_text("Unavailable / stale")
    page.keyboard.press("Escape")
    expect(readings).to_contain_text("Readings stale")
    expect(readings.locator("dd")).to_have_text(["—"] * 6)
    status["metadata_age"] = 0
    status["metadata"] = {"ExposureTime": 125, "AnalogueGain": 1, "FrameDuration": 0}
    expect(readings.locator("dd")).to_have_text(["125 µs", "1.00×", "—", "—", "—", "—"])
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot(page, "photo-camera-mobile")
    page.route(
        "**/api/status",
        lambda route: route.fulfill(status=503, json={"error": "Offline"}),
    )
    expect(readings).to_contain_text("Readings stale")
    expect(readings.locator("dd")).to_have_text(["—"] * 6)
    expect(page.get_by_role("button", name="Take photo", exact=True)).to_be_disabled()


def test_transfer_runs_and_supervises_rsync_on_the_pi(library_page, tmp_path_factory):
    page = library_page
    target = tmp_path_factory.mktemp("rsync-copy")
    page.get_by_role("link", name="Transfer", exact=True).click()
    section = page.locator(
        "section", has=page.get_by_role("heading", name="rsync", exact=True)
    )
    expect(section.get_by_role("status")).to_contain_text(
        "Not run since CameraDeck started"
    )
    start = section.get_by_role("button", name="Start rsync")
    expect(start).to_be_enabled()
    section.get_by_label("Destination").fill("")
    expect(start).to_be_disabled()
    section.get_by_label("Destination").fill("-e sh")
    start.click()
    expect(section.get_by_role("alert")).to_contain_text("must not start with '-'")
    section.get_by_label("Destination").fill(str(target))
    start.click()
    expect(section.get_by_role("status")).to_contain_text("Finished", timeout=30000)
    expect(section.get_by_role("status")).to_contain_text("Transfer complete.")
    expect(section.get_by_role("progressbar", name="rsync progress")).to_have_attribute(
        "value", "100"
    )
    expect(section.get_by_role("alert")).to_have_count(0)
    section.get_by_text("Command and log").click()
    expect(section.get_by_label("rsync log")).to_contain_text("--exclude=.*")
    names = {p.name for p in target.iterdir()}
    assert len(names) == 242 and all(n.endswith((".jpg", ".json")) for n in names)
    assert not any(n.endswith((".thumb.jpg", ".preview.jpg")) for n in names)
    screenshot(page, "transfer-pi-rsync")
    page.set_viewport_size({"width": 390, "height": 844})
    screenshot(page, "transfer-pi-rsync-mobile")
