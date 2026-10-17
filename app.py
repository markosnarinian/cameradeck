"""Local camera web server. Run one process: camera ownership is not multiprocess-safe."""

import argparse
import hmac
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    send_file,
    send_from_directory,
    session,
)
from camera import CameraDeck
from catalog import ID, MediaCatalog
from power import PowerManager
from scripts.frontend import check as check_frontend
from transfer import Busy, RsyncTransfer


class Activity:
    """Request-driven work that stopping the service would cut short."""

    def __init__(self):
        self.lock = threading.Lock()
        self.counts = Counter()
        self.exclusive = False

    def begin(self, kind, *, exclusive=False):
        with self.lock:
            if self.exclusive or (exclusive and any(self.counts.values())):
                abort(
                    409,
                    description="Wait for media operations to finish before continuing.",
                )
            if exclusive:
                self.exclusive = True
            self.counts[kind] += 1
        ended = []

        def end():
            with self.lock:
                if not ended:
                    ended.append(True)
                    self.counts[kind] -= 1
                    if exclusive:
                        self.exclusive = False

        return end

    @contextmanager
    def track(self, kind, *, exclusive=False):
        end = self.begin(kind, exclusive=exclusive)
        try:
            yield
        finally:
            end()

    def snapshot(self):
        with self.lock:
            return {kind: count for kind, count in self.counts.items() if count > 0}


def head_commit():
    """Return the source revision that started this CameraDeck process."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).parent,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def create_app(
    deck,
    password="",
    *,
    power_manager=None,
    rsync=None,
):
    app = Flask(__name__, static_folder=None)
    frontend = Path(__file__).parent / "static" / "dist"
    revision = head_commit()
    key_path = deck.root / ".session-key"
    try:
        with open(
            key_path, "x", opener=lambda path, flags: os.open(path, flags, 0o600)
        ) as key:
            key.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    app.secret_key = key_path.read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{64}", app.secret_key):
        raise RuntimeError(
            "Invalid .session-key. Remove it while the service is stopped to regenerate it."
        )
    authentication = hmac.new(
        app.secret_key.encode(), password.encode(), "sha256"
    ).hexdigest()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        SESSION_REFRESH_EACH_REQUEST=False,
    )
    attempts = {}
    attempts_lock = threading.Lock()
    viewers = threading.BoundedSemaphore(4)
    catalog = MediaCatalog(deck.root)
    app.extensions["catalog"] = catalog
    deck.media_published = catalog.publish
    catalog.start()
    power_manager = power_manager or PowerManager()
    rsync = rsync or RsyncTransfer(deck.root)
    app.extensions["rsync"] = rsync
    activity = Activity()

    def authenticated():
        return not password or session.get("authenticated") == authentication

    @app.before_request
    def protect():
        if request.path not in {
            "/api/media/range",
            "/api/media/download",
            "/api/media/delete",
        }:
            request.max_content_length = 16384
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
        if (
            not authenticated()
            and request.path
            not in {
                "/",
                "/library",
                "/transfer",
                "/api/login",
                "/api/session",
                "/favicon.ico",
            }
            and not request.path.startswith("/assets/")
        ):
            return jsonify(error="Unlock CameraDeck to continue."), 401
        if request.method not in {"GET", "HEAD", "OPTIONS", "DELETE"} and (
            not request.is_json or not isinstance(request.get_json(), dict)
        ):
            raise ValueError("Supply a JSON object.")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'"
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
    @app.get("/library")
    @app.get("/transfer")
    def index():
        return send_from_directory(frontend, "index.html", max_age=0)

    @app.get("/assets/<path:name>")
    def asset(name):
        return send_from_directory(frontend / "assets", name, max_age=31536000)

    @app.get("/api/session")
    def session_status():
        return jsonify(
            authenticated=authenticated(), required=bool(password), revision=revision
        )

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

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
        session.clear()
        session.permanent = True
        session["authenticated"] = authentication
        return jsonify(ok=True)

    @app.get("/api/status")
    def status():
        return jsonify({**deck.status(), "transfer_active": rsync.active})

    @app.get("/api/activity")
    def activity_status():
        """Whether stopping the service now would interrupt anything (see UPDATE.md)."""
        operations = activity.snapshot()
        if deck.sequence and deck.sequence["active"]:
            operations["capture_sequence"] = 1
        if rsync.active:
            operations["rsync_transfer"] = 1
        if catalog.indexing:
            operations["library_indexing"] = 1
        if deck.recovery_worker and deck.recovery_worker.is_alive():
            operations["clip_recovery"] = 1
        if getattr(power_manager, "pending", False) is True:
            operations["power_action"] = 1
        return jsonify(idle=not operations, operations=operations)

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

    @app.post("/api/capture")
    def capture():
        with activity.track("photo_capture"):
            return jsonify(deck.capture())

    @app.post("/api/sequence/start")
    def sequence_start():
        data = request.get_json()
        # deck.lock is shared with rsync start so neither can slip past the other.
        with deck.lock:
            if rsync.active:
                return (
                    jsonify(error="Stop the rsync transfer before starting capture."),
                    409,
                )
            return jsonify(
                deck.start_sequence(data.get("mode"), data.get("settings", {}))
            )

    @app.post("/api/sequence/stop")
    def sequence_stop():
        return jsonify(deck.stop_sequence())

    @app.get("/api/preview")
    def preview():
        with deck.frames.condition:
            frame, updated = deck.frames.frame, deck.frames.updated
        if not frame or not updated or time.monotonic() - updated > 3:
            return (
                jsonify(
                    error="No fresh preview frame. Check the camera and exposure settings."
                ),
                503,
            )
        return Response(
            frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store"}
        )

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
            sequence = -1
            while not deck.shutdown.is_set():
                with deck.frames.condition:
                    ready = deck.frames.condition.wait_for(
                        lambda: sequence != deck.frames.sequence
                        or deck.shutdown.is_set(),
                        timeout=10,
                    )
                    if not ready:
                        return
                    sequence, frame = deck.frames.sequence, deck.frames.frame
                    if (
                        not deck.frames.updated
                        or time.monotonic() - deck.frames.updated > 3
                    ):
                        return
                if frame:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(
                        len(frame)
                    ).encode() + b"\r\n\r\n" + frame + b"\r\n"

        response = Response(
            frames(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )
        # Release on close, not in the generator: HEAD responses never start it.
        response.call_on_close(viewers.release)
        return response

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
        return jsonify(catalog.page(kind, page, per_page))

    @app.post("/api/media/range")
    def library_range():
        body = request.get_json()
        kind = body.get("kind", "all")
        if kind not in ("all", "still", "video"):
            raise ValueError("kind must be all, still, or video.")
        start, end = body.get("start"), body.get("end")
        if any(
            not isinstance(ident, str) or not ID.fullmatch(ident)
            for ident in (start, end)
        ):
            raise ValueError("One or more media IDs are invalid.")
        identifiers = ids_from_request() if "ids" in body else None
        return jsonify(ids=catalog.range(kind, start, end, identifiers))

    @app.get("/api/media/<ident>")
    def detail(ident):
        return jsonify(item(ident))

    @app.get("/media/<ident>/<variant>")
    def media(ident, variant):
        entry = item(ident)
        downloading = variant == "original" and "download" in request.args
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
        end = activity.begin("download") if downloading else None
        try:
            response = send_file(
                path,
                conditional=True,
                as_attachment="download" in request.args,
                download_name=name,
                max_age=86400,
            )
        except Exception:
            if end:
                end()
            raise
        if end:
            # Iterate in the request thread so close marks the end of sending.
            response.direct_passthrough = False
            response.call_on_close(end)
        response.cache_control.public = False
        response.cache_control.private = True
        return response

    @app.delete("/api/media/<ident>")
    def delete(ident):
        with activity.track("deletion"), deck.lock, catalog.lock:
            entry = item(ident)
            media_path(entry)
            for name in [
                entry["file"],
                ident + ".thumb.jpg",
                ident + ".preview.jpg",
                ident + ".json",
            ]:
                (deck.root / name).unlink(missing_ok=True)
            catalog.remove(ident)
        return jsonify(ok=True)

    @app.post("/api/media/delete")
    def delete_many():
        with activity.track("deletion"):
            return delete_entries(ids_from_request())

    @app.post("/api/media/delete-all")
    def delete_all():
        confirmation = request.get_json().get("confirm")
        if not isinstance(confirmation, str) or confirmation.casefold() != "delete all":
            raise ValueError("Type DELETE ALL to confirm.")
        # Keep capture/rsync starts out until the entire deletion is complete.
        with deck.lock:
            if (
                (deck.sequence and deck.sequence["active"])
                or rsync.active
                or catalog.indexing
                or (deck.recovery_worker and deck.recovery_worker.is_alive())
                or getattr(power_manager, "pending", False) is True
            ):
                abort(
                    409,
                    description="CameraDeck must be idle before deleting all media.",
                )
            with activity.track("deletion", exclusive=True):
                identifiers = [
                    path.stem
                    for path in deck.root.glob("*.json")
                    if ID.fullmatch(path.stem) and path.is_file()
                ]
                # Completed sidecars can be empty/corrupt. Use only CameraDeck's
                # filenames, never paths read from damaged metadata or a glob
                # that could also remove pending clips or unrelated files.
                for ident in identifiers:
                    with catalog.lock:
                        for suffix in (
                            ".jpg",
                            ".mp4",
                            ".thumb.jpg",
                            ".preview.jpg",
                            ".json",
                        ):
                            (deck.root / (ident + suffix)).unlink(missing_ok=True)
                        catalog.remove(ident)
                return jsonify(deleted=identifiers, missing=[])

    def delete_entries(identifiers):
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
        for ident, entry, original in entries:
            with deck.lock, catalog.lock:
                for name in [
                    original.name,
                    ident + ".thumb.jpg",
                    ident + ".preview.jpg",
                    ident + ".json",
                ]:
                    (deck.root / name).unlink(missing_ok=True)
                catalog.remove(ident)
        return jsonify(deleted=[ident for ident, _, _ in entries], missing=missing)

    @app.post("/api/media/download")
    def download_many():
        identifiers = ids_from_request()
        entries = [(ident, item(ident)) for ident in identifiers]
        end = activity.begin("zip_download")
        temporary = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        temporary.close()
        try:
            with zipfile.ZipFile(
                temporary.name, "w", compression=zipfile.ZIP_STORED, allowZip64=True
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
            # Keep Response.close in the WSGI iterator's cleanup path.
            response.direct_passthrough = False
            response.call_on_close(lambda: Path(temporary.name).unlink(missing_ok=True))
            response.call_on_close(end)
            return response
        except Exception:
            Path(temporary.name).unlink(missing_ok=True)
            end()
            raise

    @app.get("/api/transfer/rsync")
    def rsync_status():
        return jsonify(rsync.status())

    @app.post("/api/transfer/rsync/start")
    def rsync_start():
        destination = request.get_json().get("destination")
        with deck.lock:
            if deck.sequence and deck.sequence["active"]:
                return jsonify(error="Stop capture before starting rsync."), 409
            try:
                return jsonify(rsync.start(destination)), 202
            except Busy as exc:
                return jsonify(error=str(exc)), 409

    @app.post("/api/transfer/rsync/stop")
    def rsync_stop():
        return jsonify(rsync.stop())

    @app.post("/api/system/power")
    def system_power():
        data = request.get_json()
        action = data.get("action")
        if action not in {"shutdown", "reboot"}:
            raise ValueError("Choose shutdown or reboot.")
        confirmation = data.get("confirm")
        if not isinstance(confirmation, str) or confirmation.casefold() != action:
            raise ValueError(f"Type {action.upper()} to confirm.")
        with deck.lock:
            if deck.sequence and deck.sequence["active"]:
                return (
                    jsonify(
                        error="Stop the capture sequence before changing Pi power."
                    ),
                    409,
                )
            if rsync.active:
                return (
                    jsonify(error="Stop the rsync transfer before changing Pi power."),
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
    check_frontend()
    logging.basicConfig(level=logging.INFO)
    deck = CameraDeck(args.media)
    app = create_app(deck, password)
    deck.start()

    def stop(*_):
        app.extensions["rsync"].close()
        deck.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    from waitress import serve

    try:
        serve(
            app,
            host=args.host,
            port=args.port,
            threads=12,
        )
    finally:
        app.extensions["rsync"].close()
        deck.close()
        app.extensions["catalog"].close()


if __name__ == "__main__":
    main()
