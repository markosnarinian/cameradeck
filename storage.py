"""S3-compatible uploads with durable, content-based at-most-once delivery."""

import hashlib
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError, NoCredentialsError, ParamValidationError


@dataclass
class UploadResult:
    status: str
    key: str
    error: str | None = None

    def json(self):
        return asdict(self)


class S3Uploader:
    """Upload originals once per byte content and destination.

    Ambiguous attempts are intentionally never retried automatically. A later call only
    reconciles the deterministic object with HEAD, preserving the at-most-once promise.
    """

    def __init__(self, root, client_factory=None):
        self.root = Path(root).resolve()
        self.database = self.root / ".uploads.sqlite3"
        self.client_factory = client_factory or self._client
        self.lock = threading.Lock()
        self._initialize()

    @staticmethod
    def _client(endpoint):
        import boto3

        return boto3.client("s3", endpoint_url=endpoint)

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS uploads (
                    sha256 TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('attempting', 'uploaded', 'uncertain')),
                    remote_etag TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (sha256, destination)
                )
                """)

    @staticmethod
    def _digest(file):
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _destination(endpoint, bucket, prefix):
        return "\n".join((endpoint.rstrip("/"), bucket, prefix.strip("/")))

    @staticmethod
    def _remote_matches(client, bucket, key, digest, size):
        try:
            response = client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        metadata = {k.lower(): v for k, v in response.get("Metadata", {}).items()}
        return (
            response.get("ContentLength") == size
            and metadata.get("cameradeck-sha256") == digest
        ), response.get("ETag")

    def _set_state(self, digest, destination, state, etag=None):
        with self._connect() as db:
            db.execute(
                "UPDATE uploads SET state=?, remote_etag=?, updated_at=? "
                "WHERE sha256=? AND destination=?",
                (
                    state,
                    etag,
                    datetime.now(timezone.utc).isoformat(),
                    digest,
                    destination,
                ),
            )

    def upload(self, path, source_id, *, endpoint_url, bucket, prefix=""):
        path = Path(path).resolve()
        if path.parent != self.root or not path.is_file():
            raise ValueError("Upload source is not a completed media original.")

        with path.open("rb") as source:
            before = path.stat()
            digest, size = self._digest(source)
            after = path.stat()
            identity = lambda stat: (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
            )
            if identity(before) != identity(after) or size != after.st_size:
                raise ValueError("The media file changed while it was being prepared.")
            source.seek(0)
            prefix = prefix.strip("/")
            name = f"sha256/{digest[:2]}/{digest}{path.suffix.lower()}"
            key = f"{prefix}/{name}" if prefix else name
            destination = self._destination(endpoint_url, bucket, prefix)

            with self.lock:
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT * FROM uploads WHERE sha256=? AND destination=?",
                        (digest, destination),
                    ).fetchone()
                    if row and row["state"] == "uploaded":
                        return UploadResult("deduplicated", row["object_key"])

                client = self.client_factory(endpoint_url)
                if row:
                    try:
                        match = self._remote_matches(
                            client, bucket, row["object_key"], digest, size
                        )
                    except Exception as exc:
                        return UploadResult("uncertain", row["object_key"], str(exc))
                    if match and match[0]:
                        self._set_state(digest, destination, "uploaded", match[1])
                        return UploadResult("reconciled", row["object_key"])
                    error = (
                        "Remote object is missing."
                        if match is None
                        else "Remote object does not match."
                    )
                    return UploadResult("uncertain", row["object_key"], error)

                try:
                    existing = self._remote_matches(client, bucket, key, digest, size)
                except Exception as exc:
                    return UploadResult(
                        "error", key, f"Could not inspect destination: {exc}"
                    )
                if existing:
                    if existing[0]:
                        now = datetime.now(timezone.utc).isoformat()
                        with self._connect() as db:
                            db.execute(
                                "INSERT INTO uploads VALUES (?, ?, ?, ?, ?, 'uploaded', ?, ?, ?)",
                                (
                                    digest,
                                    destination,
                                    key,
                                    source_id,
                                    size,
                                    existing[1],
                                    now,
                                    now,
                                ),
                            )
                        return UploadResult("reconciled", key)
                    return UploadResult(
                        "error", key, "Destination key contains different content."
                    )

                now = datetime.now(timezone.utc).isoformat()
                with self._connect() as db:
                    db.execute(
                        "INSERT INTO uploads VALUES (?, ?, ?, ?, ?, 'attempting', NULL, ?, ?)",
                        (digest, destination, key, source_id, size, now, now),
                    )
                try:
                    client.put_object(
                        Bucket=bucket,
                        Key=key,
                        Body=source,
                        ContentLength=size,
                        Metadata={
                            "cameradeck-sha256": digest,
                            "cameradeck-id": source_id,
                        },
                        IfNoneMatch="*",
                    )
                except Exception as exc:
                    status = (
                        exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        if isinstance(exc, ClientError)
                        else None
                    )
                    # A 4xx response is a definite rejection, so nothing was stored and a
                    # later attempt is safe. 409/412 mean a conditional write raced another
                    # and stay uncertain, like lost responses, to be reconciled with HEAD.
                    if isinstance(exc, (NoCredentialsError, ParamValidationError)) or (
                        isinstance(status, int)
                        and 400 <= status < 500
                        and status not in (409, 412)
                    ):
                        with self._connect() as db:
                            db.execute(
                                "DELETE FROM uploads WHERE sha256=? AND destination=?",
                                (digest, destination),
                            )
                        return UploadResult("error", key, str(exc))
                    self._set_state(digest, destination, "uncertain")
                    return UploadResult("uncertain", key, str(exc))

                try:
                    match = self._remote_matches(client, bucket, key, digest, size)
                except Exception as exc:
                    self._set_state(digest, destination, "uncertain")
                    return UploadResult("uncertain", key, str(exc))
                if not match or not match[0]:
                    self._set_state(digest, destination, "uncertain")
                    return UploadResult(
                        "uncertain", key, "Uploaded object could not be verified."
                    )
                self._set_state(digest, destination, "uploaded", match[1])
                return UploadResult("uploaded", key)
