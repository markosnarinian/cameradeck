"""Explicit hardware/browser test against an already-running CameraDeck server.

Run: .venv/bin/python tests/browser_smoke.py
Uses the real camera; temporarily changes controls and records clips. Only deletes
captures created by this test. Screenshots stay in ignored test-results/.
"""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = os.environ.get("CAMERADECK_TEST_URL", "http://127.0.0.1:8080")
OUT = Path("test-results")
OUT.mkdir(exist_ok=True)


def run():
    created = []
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("CHROMIUM", "/usr/bin/chromium"),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(URL, wait_until="domcontentloaded")
        if os.environ.get("CAMERADECK_PASSWORD"):
            page.locator("#password").fill(os.environ["CAMERADECK_PASSWORD"])
            page.get_by_text("Unlock CameraDeck", exact=True).click()
        expect(page.locator("#capture")).to_be_enabled(timeout=15000)
        initial = context.request.get(URL + "/api/status").json()

        def post(path, data=None):
            r = context.request.post(URL + path, data=data or {}, timeout=45000)
            assert r.ok, (path, r.status, r.text())
            return r.json()

        def capture():
            with page.expect_response(
                lambda r: r.url.endswith("/api/capture") and r.request.method == "POST",
                timeout=45000,
            ) as response:
                page.locator("#capture").click()
            assert response.value.ok, response.value.text()
            item = response.value.json()
            created.append(item["id"])
            expect(page.locator("#capture")).to_be_enabled(timeout=15000)
            return item

        try:
            page.locator("#profile").select_option("1080p")
            page.locator("#fps").fill("30")
            page.locator("#rotation").select_option("180")
            page.locator("#apply-config").click()
            expect(page.locator("#capture")).to_be_enabled(timeout=15000)
            page.wait_for_function(
                "() => document.getElementById('feed').naturalWidth === 640"
            )
            page.locator("#focus-toggle").check()
            expect(page.locator("#live-grid .focus-cell")).to_have_count(
                9, timeout=10000
            )
            config = context.request.get(URL + "/api/status").json()
            assert config["rotation"] == 180 and config["profile"] == "1080p"
            assert 1 <= config["fps"] <= 30
            assert page.locator("#advanced-select option").count() == len(
                config["controls"]
            )

            page.locator("#control-ExposureTime").fill("8000")
            page.locator("#control-ExposureTime").press("Tab")
            page.wait_for_timeout(1800)
            metadata = context.request.get(URL + "/api/status").json()["metadata"]
            assert abs(metadata["ExposureTime"] - 8000) < 300, metadata["ExposureTime"]
            post(
                "/api/controls",
                {
                    k: v
                    for k, v in {
                        "AeEnable": True,
                        "ExposureTimeMode": 0,
                        "AnalogueGainMode": 0,
                    }.items()
                    if k in config["controls"]
                },
            )
            still = capture()
            assert [still["width"], still["height"]] == config["properties"][
                "PixelArraySize"
            ]
            page.screenshot(path=str(OUT / "desktop-live.png"))

            with page.expect_response(
                lambda r: r.url.endswith("/api/record/start")
            ) as response:
                page.locator("#record").click()
            assert response.value.ok, response.value.text()
            recording = response.value.json()
            created.append(recording["id"])
            expect(page.locator("#record-label")).to_have_text(
                "Stop recording", timeout=10000
            )
            assert (
                context.request.post(
                    URL + "/api/configure", data={"profile": "720p"}
                ).status
                == 400
            )
            page.wait_for_timeout(2500)
            snapshot = capture()
            assert [snapshot["width"], snapshot["height"]] == [1920, 1080]
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=str(OUT / "mobile-recording.png"))
            page.locator("#library-tab").click()
            expect(page.locator("#feed")).not_to_have_attribute("src", "/stream.mjpg")
            with page.expect_response(
                lambda r: r.url.endswith("/api/record/stop"), timeout=30000
            ) as response:
                page.locator("#global-stop").click()
            assert response.value.ok, response.value.text()
            video = response.value.json()
            assert video["duration"] >= 2.5, video
            expect(page.locator("#global-stop")).to_be_hidden(timeout=10000)

            page.locator('[data-kind="video"]').click()
            expect(page.locator("#library-grid .media-card").first).to_be_visible()
            page.locator("#library-grid .media-card").first.click()
            page.wait_for_function(
                "() => document.getElementById('viewer-video').readyState >= 2"
            )
            page.locator("#viewer-video").evaluate("(v)=>v.play()")
            page.wait_for_timeout(1200)
            assert page.locator("#viewer-video").evaluate("(v)=>v.currentTime") > 0.3
            page.locator("#viewer-video").evaluate("(v)=>{v.pause();v.currentTime=1;}")
            page.locator("#viewer-focus").check()
            expect(page.locator("#viewer-grid .focus-cell")).to_have_count(9)
            page.screenshot(path=str(OUT / "mobile-video-focus.png"))
            with page.expect_download() as download:
                page.locator("#download").click()
            download.value.save_as(str(OUT / "hardware-video.mp4"))
            page.locator("#close-viewer").click()

            page.locator('[data-kind="still"]').click()
            page.wait_for_timeout(700)
            page.screenshot(path=str(OUT / "mobile-library.png"))
            page.locator("#library-grid .media-card").first.click()
            page.locator("#viewer-focus").check()
            expect(page.locator("#viewer-grid .focus-cell")).to_have_count(9)
            page.locator("#load-original").click()
            expect(page.locator("#load-original")).to_have_text(
                "Full resolution loaded", timeout=15000
            )
            assert page.locator("#viewer-image").evaluate("(i)=>i.naturalWidth") == 1920
            page.screenshot(path=str(OUT / "mobile-still-focus.png"))
            with page.expect_download() as download:
                page.locator("#download-metadata").click()
            download.value.save_as(str(OUT / "hardware-metadata.json"))
            page.on("dialog", lambda d: d.accept())
            with page.expect_response(
                lambda r: "/api/media/" in r.url and r.request.method == "DELETE"
            ) as response:
                page.locator("#delete-media").click()
            assert response.value.ok
            expect(page.locator("#viewer")).not_to_be_visible()
            assert (
                context.request.get(URL + "/api/media/" + snapshot["id"]).status == 404
            )
            created.remove(snapshot["id"])

            page.locator("#live-tab").click()
            page.evaluate("window.scrollTo(0,0)")
            page.wait_for_timeout(1600)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(OUT / "mobile-live.png"))
            page.locator("#sensor-info").evaluate("(e)=>e.parentElement.open=true")
            page.locator("#sensor-info").scroll_into_view_if_needed()
            assert page.locator("#capture").bounding_box()["y"] < 844
            page.evaluate("window.scrollTo(0,0)")
            page.set_viewport_size({"width": 1440, "height": 1100})
            page.screenshot(
                path=str(OUT / "desktop-private.png"),
                mask=[page.locator("#feed"), page.locator("#recent img")],
                mask_color="#263b30",
            )
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "sensor": config["properties"]["Model"],
                        "controls": len(config["controls"]),
                        "fps": config["fps"],
                        "still": [still["width"], still["height"]],
                        "snapshot": [snapshot["width"], snapshot["height"]],
                        "video_duration": video["duration"],
                        "js_errors": errors,
                    },
                    indent=2,
                )
            )
        finally:
            if context.request.get(URL + "/api/status").json().get("recording"):
                post("/api/record/stop")
            for ident in created:
                context.request.delete(URL + "/api/media/" + ident, data={})
            post(
                "/api/configure",
                {k: initial[k] for k in ["index", "profile", "fps", "rotation"]},
            )
            post("/api/focus", {"enabled": initial["focus_enabled"]})
            if initial["applied"]:
                post("/api/controls", initial["applied"])
            browser.close()


if __name__ == "__main__":
    run()
