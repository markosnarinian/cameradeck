"""Local camera web server. Run one process: camera ownership is not multiprocess-safe."""

import argparse
import hmac
import json
import logging
import math
import os
import re
import secrets
import signal
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, request, send_file, session
from camera import CameraDeck
from power import PowerManager

ID = re.compile(r"^\d{8}_\d{6}_[a-f0-9]{8}$")
BUCKET = re.compile(r"^(?!\d+\.\d+\.\d+\.\d+$)[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
MAX_BATCH = 100


def normalize_endpoint(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter an S3-compatible endpoint URL.")
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Endpoint must be an HTTP(S) server URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "Endpoint must not contain credentials, a query, or a fragment."
        )
    if parsed.path not in {"", "/"}:
        raise ValueError(
            "Enter the server endpoint only; use Bucket and Prefix separately."
        )
    insecure_allowed = parsed.hostname == "localhost"
    try:
        insecure_allowed = insecure_allowed or ip_address(parsed.hostname).is_private
    except ValueError:
        pass
    if parsed.scheme == "http" and not insecure_allowed:
        raise ValueError("S3 endpoints must use HTTPS outside private networks.")
    return value.rstrip("/")


def create_app(
    deck,
    password="",
    *,
    uploader=None,
    power_manager=None,
    s3_endpoint="http://192.168.1.10:3900",
):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.secret_key = secrets.token_hex(32)
    app.config.update(
        MAX_CONTENT_LENGTH=16384,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    attempts = {}
    attempts_lock = threading.Lock()
    viewers = threading.BoundedSemaphore(4)
    power_manager = power_manager or PowerManager()
    configured_endpoint = normalize_endpoint(s3_endpoint)

    @app.before_request
    def protect():
        if not password and urlsplit(request.host_url).hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            abort(403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin and (
                urlsplit(origin).netloc != request.host
                or urlsplit(origin).scheme != request.scheme
            ):
                abort(403)
            if request.method != "DELETE" and (
                not request.is_json or not isinstance(request.get_json(), dict)
            ):
                raise ValueError("Supply a JSON object.")
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

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

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

    ATHENS = ZoneInfo("Europe/Athens")

    @app.get("/api/time")
    def get_time():
        now = datetime.now(timezone.utc)
        corrected = now + timedelta(seconds=deck.time_offset)
        return jsonify(
            utc=now.isoformat(),
            athens=corrected.astimezone(ATHENS).isoformat(),
            epoch=int(now.timestamp()),
            offset=round(deck.time_offset, 3),
        )

    @app.post("/api/time")
    def set_time():
        t = request.get_json().get("time", "")
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?", t)
        if not m:
            raise ValueError("Time must be ISO format, e.g. 2026-09-07T18:30:00")
        dt = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M").replace(
            tzinfo=ATHENS
        )
        deck.save_offset(dt.timestamp() - datetime.now(timezone.utc).timestamp())
        return jsonify(ok=True)

    @app.post("/api/configure")
    def configure():
        data = request.get_json()
        deck.open(
            int(data.get("index", deck.index)),
            data.get("profile", deck.profile),
            float(data.get("fps", deck.fps)),
            int(data.get("rotation", deck.rotation)),
        )
        return jsonify(deck.status())

    @app.post("/api/controls")
    def controls():
        deck.set_controls(request.get_json())
        return jsonify(ok=True)

    @app.post("/api/focus")
    def focus():
        enabled = request.get_json().get("enabled")
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false.")
        deck.focus_enabled = enabled
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

    @app.post("/api/sequence/start")
    def sequence_start():
        data = request.get_json()
        return jsonify(deck.start_sequence(data.get("mode"), data.get("settings", {})))

    @app.post("/api/sequence/stop")
    def sequence_stop():
        return jsonify(deck.stop_sequence())

    @app.get("/stream.mjpg")
    def stream():
        if not viewers.acquire(blocking=False):
            return (
                jsonify(
                    error="Four live viewers are already connected. Close another live view."
                ),
                503,
            )

        def frames():
            try:
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
            finally:
                viewers.release()

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
        entry = json.loads(path.read_text())
        if entry.get("id") != ident:
            abort(404)
        return entry

    def media_path(entry):
        name = entry.get("file")
        if not isinstance(name, str) or Path(name).name != name:
            abort(404)
        path = (deck.root / name).resolve()
        if path.parent != deck.root or not path.is_file():
            abort(404)
        return path

    def ids_from_request():
        identifiers = request.get_json().get("ids")
        if not isinstance(identifiers, list) or not identifiers:
            raise ValueError("Select at least one library item.")
        if len(identifiers) > MAX_BATCH:
            raise ValueError(f"Select no more than {MAX_BATCH} items at once.")
        if any(
            not isinstance(ident, str) or not ID.fullmatch(ident)
            for ident in identifiers
        ):
            raise ValueError("One or more media IDs are invalid.")
        return list(dict.fromkeys(identifiers))

    def positive_integer(name, default, maximum=None):
        value = request.args.get(name, str(default))
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a positive integer.")
        if value < 1:
            raise ValueError(f"{name} must be a positive integer.")
        return min(value, maximum) if maximum else value

    @app.get("/api/media")
    def library():
        kind = request.args.get("kind", "all")
        if kind not in {"all", "still", "video"}:
            raise ValueError("kind must be all, still, or video.")
        page = positive_integer("page", 1)
        per_page = positive_integer("per_page", 40, 100)
        items = []
        for path in sorted(deck.root.glob("*.json"), reverse=True):
            if not ID.fullmatch(path.stem):
                continue
            try:
                entry = json.loads(path.read_text())
                if kind == "all" or entry["kind"] == kind:
                    items.append(entry)
            except (OSError, ValueError, KeyError):
                continue
        total = len(items)
        pages = math.ceil(total / per_page)
        if pages:
            page = min(page, pages)
        start = (page - 1) * per_page
        return jsonify(
            items=items[start : start + per_page],
            page=page,
            per_page=per_page,
            pages=pages,
            total=total,
        )

    @app.get("/api/media/<ident>")
    def detail(ident):
        return jsonify(item(ident))

    @app.get("/media/<ident>/<variant>")
    def media(ident, variant):
        entry = item(ident)
        if variant == "original":
            path = media_path(entry)
            name = path.name
        elif variant in {"thumb", "preview"}:
            name = ident + "." + variant + ".jpg"
            path = deck.root / name
        elif variant == "metadata":
            name = ident + ".json"
            path = deck.root / name
        else:
            abort(404)
        response = send_file(
            path,
            conditional=True,
            as_attachment="download" in request.args,
            download_name=name,
            max_age=86400,
        )
        response.cache_control.public = False
        response.cache_control.private = True
        return response

    @app.delete("/api/media/<ident>")
    def delete(ident):
        with deck.lock:
            entry = item(ident)
            media_path(entry)
            for name in [
                entry["file"],
                ident + ".thumb.jpg",
                ident + ".preview.jpg",
                ident + ".json",
            ]:
                (deck.root / name).unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.post("/api/media/delete")
    def delete_many():
        identifiers = ids_from_request()
        entries, missing = [], []
        for ident in identifiers:
            try:
                entry = item(ident)
                entries.append((ident, entry, media_path(entry)))
            except Exception as exc:
                from werkzeug.exceptions import NotFound

                if isinstance(exc, NotFound):
                    missing.append(ident)
                else:
                    raise
        with deck.lock:
            for ident, entry, original in entries:
                for name in [
                    original.name,
                    ident + ".thumb.jpg",
                    ident + ".preview.jpg",
                    ident + ".json",
                ]:
                    (deck.root / name).unlink(missing_ok=True)
        return jsonify(deleted=[ident for ident, _, _ in entries], missing=missing)

    @app.post("/api/media/download")
    def download_many():
        identifiers = ids_from_request()
        entries = [(ident, item(ident)) for ident in identifiers]
        temporary = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        temporary.close()
        try:
            with zipfile.ZipFile(
                temporary.name, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for ident, entry in entries:
                    original = media_path(entry)
                    archive.write(original, f"{ident}/{original.name}")
                    archive.write(deck.root / f"{ident}.json", f"{ident}/{ident}.json")
            response = send_file(
                temporary.name,
                mimetype="application/zip",
                as_attachment=True,
                download_name="cameradeck-media.zip",
                max_age=0,
            )
            response.call_on_close(lambda: Path(temporary.name).unlink(missing_ok=True))
            return response
        except Exception:
            Path(temporary.name).unlink(missing_ok=True)
            raise

    @app.get("/api/settings")
    def settings():
        return jsonify(s3_endpoint=configured_endpoint)

    @app.post("/api/media/upload")
    def upload_many():
        nonlocal uploader
        data = request.get_json()
        identifiers = ids_from_request()
        endpoint = normalize_endpoint(data.get("endpoint", configured_endpoint))
        bucket = data.get("bucket", "")
        prefix = data.get("prefix", "")
        if not isinstance(bucket, str) or not BUCKET.fullmatch(bucket):
            raise ValueError("Enter a valid S3 bucket name.")
        if (
            not isinstance(prefix, str)
            or any(part in {".", ".."} for part in prefix.split("/"))
            or any(ord(char) < 32 for char in prefix)
        ):
            raise ValueError("Prefix contains unsupported path segments.")
        if uploader is None:
            from storage import S3Uploader

            uploader = S3Uploader(deck.root)
        results = []
        for ident in identifiers:
            try:
                entry = item(ident)
                result = uploader.upload(
                    media_path(entry),
                    ident,
                    endpoint_url=endpoint,
                    bucket=bucket,
                    prefix=prefix.strip("/"),
                )
                value = result.json() if hasattr(result, "json") else dict(result)
            except Exception as exc:
                value = {"status": "error", "key": None, "error": str(exc)}
            results.append({"id": ident, **value})
        return jsonify(results=results)

    @app.post("/api/system/power")
    def system_power():
        data = request.get_json()
        action = data.get("action")
        if action not in {"shutdown", "reboot"}:
            raise ValueError("Choose shutdown or reboot.")
        confirmation = data.get("confirm")
        if not isinstance(confirmation, str) or confirmation.casefold() != action:
            raise ValueError(f"Type {action.upper()} to confirm.")
        if deck.recording or (deck.sequence and deck.sequence["active"]):
            return (
                jsonify(
                    error="Stop recording or the capture sequence before changing Pi power."
                ),
                409,
            )
        try:
            power_manager.schedule(action)
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(accepted=True, action=action), 202

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
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not password:
        parser.error("Network access requires CAMERADECK_PASSWORD to be set.")
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
            low_space = deck.status()["free_bytes"] < 256 * 1024 * 1024
            if deck.recording and (low_space or deck.output_error):
                try:
                    deck.stop_recording()
                    deck.error = "Recording stopped: " + (
                        "storage is nearly full."
                        if low_space
                        else str(deck.output_error)
                    )
                except Exception:
                    logging.exception("Could not finalize recording")

    threading.Thread(target=watchdog, daemon=True).start()
    from waitress import serve

    try:
        serve(
            create_app(
                deck,
                password,
                s3_endpoint=os.environ.get(
                    "CAMERADECK_S3_ENDPOINT", "http://192.168.1.10:3900"
                ),
            ),
            host=args.host,
            port=args.port,
            threads=12,
        )
    finally:
        deck.close()


if __name__ == "__main__":
    main()
