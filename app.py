"""Local camera web server. Run one process: camera ownership is not multiprocess-safe."""

import argparse
import hmac
import json
import logging
import os
import re
import secrets
import signal
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, jsonify, request, send_file, session
from camera import CameraDeck

ID = re.compile(r"^\d{8}_\d{6}_[a-f0-9]{8}$")


def create_app(deck, password=""):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.secret_key = secrets.token_hex(32)
    app.config.update(
        MAX_CONTENT_LENGTH=16384,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    attempts = {}
    attempts_lock = threading.Lock()

    @app.before_request
    def protect():
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                abort(403)
        if (
            password
            and not session.get("authenticated")
            and request.path
            not in {"/", "/api/login", "/static/style.css", "/static/app.js"}
        ):
            return jsonify(error="Unlock CameraDeck to continue."), 401

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        )
        if request.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(Exception)
    def failure(exc):
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        app.logger.exception("Camera operation failed")
        return jsonify(error=str(exc)), 500

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.post("/api/login")
    def login():
        address = request.remote_addr
        now = time.monotonic()
        with attempts_lock:
            for key in list(attempts):
                attempts[key] = [t for t in attempts[key] if now - t < 60]
                if not attempts[key]:
                    del attempts[key]
            if len(attempts.get(address, [])) >= 10:
                return jsonify(error="Too many attempts. Wait one minute."), 429
            supplied = request.get_json().get("password", "")
            if not isinstance(supplied, str) or not hmac.compare_digest(
                supplied.encode(), password.encode()
            ):
                attempts.setdefault(address, []).append(now)
                return jsonify(error="Incorrect password."), 401
            attempts.pop(address, None)
        session["authenticated"] = True
        return jsonify(ok=True)

    @app.get("/api/status")
    def status():
        return jsonify(deck.status())

    @app.post("/api/configure")
    def configure():
        data = request.get_json()
        deck.open(
            int(data.get("index", deck.index)),
            data.get("profile", deck.profile),
            float(data.get("fps", deck.fps)),
        )
        return jsonify(deck.status())

    @app.post("/api/controls")
    def controls():
        deck.set_controls(request.get_json())
        return jsonify(ok=True)

    @app.post("/api/focus")
    def focus():
        deck.focus_enabled = bool(request.get_json().get("enabled"))
        return jsonify(ok=True)

    @app.post("/api/capture")
    def capture():
        return jsonify(deck.capture())

    @app.post("/api/record/start")
    def record_start():
        return jsonify(deck.start_recording())

    @app.post("/api/record/stop")
    def record_stop():
        return jsonify(deck.stop_recording())

    @app.get("/stream.mjpg")
    def stream():
        def frames():
            sequence = -1
            while not deck.shutdown.is_set():
                with deck.frames.condition:
                    ready = deck.frames.condition.wait_for(
                        lambda: sequence != deck.frames.sequence
                        or deck.shutdown.is_set(),
                        timeout=10,
                    )
                    if not ready:
                        continue
                    sequence, frame = deck.frames.sequence, deck.frames.frame
                if frame:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(
                        len(frame)
                    ).encode() + b"\r\n\r\n" + frame + b"\r\n"

        return Response(
            frames(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )

    def item(ident):
        if not ID.fullmatch(ident):
            abort(404)
        path = deck.root / (ident + ".json")
        if not path.is_file():
            abort(404)
        return json.loads(path.read_text())

    @app.get("/api/media")
    def library():
        offset = max(0, request.args.get("offset", 0, type=int))
        kind = request.args.get("kind", "all")
        items = []
        for path in sorted(deck.root.glob("*.json"), reverse=True):
            try:
                entry = json.loads(path.read_text())
                if kind == "all" or entry["kind"] == kind:
                    items.append(entry)
            except (OSError, ValueError, KeyError):
                continue
        return jsonify(items=items[offset : offset + 40], total=len(items))

    @app.get("/api/media/<ident>")
    def detail(ident):
        return jsonify(item(ident))

    @app.get("/media/<ident>/<variant>")
    def media(ident, variant):
        entry = item(ident)
        if variant == "original":
            name = entry["file"]
        elif variant in {"thumb", "preview"}:
            name = ident + "." + variant + ".jpg"
        elif variant == "metadata":
            name = ident + ".json"
        else:
            abort(404)
        return send_file(
            deck.root / name,
            conditional=True,
            as_attachment="download" in request.args,
            download_name=name,
            max_age=86400,
        )

    @app.delete("/api/media/<ident>")
    def delete(ident):
        with deck.lock:
            entry = item(ident)
            for name in [
                entry["file"],
                ident + ".thumb.jpg",
                ident + ".preview.jpg",
                ident + ".json",
            ]:
                (deck.root / name).unlink(missing_ok=True)
        return jsonify(ok=True)

    return app


def main():
    parser = argparse.ArgumentParser(
        description="CameraDeck · Raspberry Pi camera console"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--media", default=str(Path(__file__).parent / "media"))
    args = parser.parse_args()
    password = os.environ.get("CAMERADECK_PASSWORD", "")
    if args.host not in {"127.0.0.1", "::1", "localhost"} and len(password) < 8:
        parser.error(
            "Network access requires CAMERADECK_PASSWORD with at least 8 characters."
        )
    logging.basicConfig(level=logging.INFO)
    deck = CameraDeck(args.media)
    deck.start()

    def stop(*_):
        deck.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # Monitor independently of clients: stop before exhausting storage, even if a phone disconnects.
    def watchdog():
        while not deck.shutdown.wait(2):
            if deck.recording and deck.status()["free_bytes"] < 256 * 1024 * 1024:
                try:
                    deck.stop_recording()
                    deck.error = "Recording stopped: storage is nearly full."
                except Exception:
                    logging.exception("Could not finalize recording")

    threading.Thread(target=watchdog, daemon=True).start()
    from waitress import serve

    try:
        serve(create_app(deck, password), host=args.host, port=args.port, threads=12)
    finally:
        deck.close()


if __name__ == "__main__":
    main()
