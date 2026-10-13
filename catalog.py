"""Persistent media index. Directory reconciliation never runs in an HTTP request."""

import json
import logging
import os
import re
import sqlite3
import threading
from uuid import uuid4

ID = re.compile(r"^\d{8}_\d{6}_[a-f0-9]{8}$")


class MediaCatalog:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.finished = threading.Event()
        self.indexing = True
        self.error = None
        self.closed = False
        self.changed = set()
        self.generation = uuid4().hex
        self.db = sqlite3.connect(root / ".library.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        # Only an index: completed JSON sidecars remain the recovery source of truth.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, entry TEXT NOT NULL,
                mtime INTEGER NOT NULL, size INTEGER NOT NULL, generation TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS media_kind_id ON media(kind, id DESC);
            """)
        # WAL readers must not wait behind the importer's sidecar I/O/write lock.
        self.reader = sqlite3.connect(
            root / ".library.sqlite3", check_same_thread=False
        )
        self.read_lock = threading.Lock()
        self.worker = threading.Thread(target=self._reconcile, daemon=True)

    def start(self):
        self.worker.start()

    def publish(self, item):
        """Called only after the complete sidecar is atomically published."""
        path = self.root / (item["id"] + ".json")
        with self.lock, self.db:
            stat = path.stat()
            self.db.execute(
                "INSERT OR REPLACE INTO media VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item["id"],
                    item["kind"],
                    json.dumps(item),
                    stat.st_mtime_ns,
                    stat.st_size,
                    self.generation,
                ),
            )
            if self.indexing:
                self.changed.add(item["id"])

    def remove(self, ident):
        with self.lock, self.db:
            self.db.execute("DELETE FROM media WHERE id = ?", (ident,))
            if self.indexing:
                self.changed.add(ident)

    def page(self, kind, page, per_page):
        where = " WHERE kind = ?" if kind != "all" else ""
        args = (kind,) if kind != "all" else ()
        indexing = self.indexing
        with self.read_lock:
            self.reader.execute("BEGIN")
            try:
                total = self.reader.execute(
                    "SELECT COUNT(*) FROM media" + where, args
                ).fetchone()[0]
                pages = (total + per_page - 1) // per_page
                page = min(page, pages) if pages else 1
                rows = self.reader.execute(
                    "SELECT entry FROM media"
                    + where
                    + " ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*args, per_page, (page - 1) * per_page),
                ).fetchall()
                indexed = (
                    total
                    if kind == "all"
                    else self.reader.execute("SELECT COUNT(*) FROM media").fetchone()[0]
                )
            finally:
                self.reader.rollback()
        return dict(
            items=[json.loads(row[0]) for row in rows],
            page=page,
            per_page=per_page,
            pages=pages,
            total=total,
            indexed=indexed,
            indexing=indexing,
            index_error=self.error,
        )

    def _commit_import(self, rows, unchanged):
        with self.lock, self.db:
            # Publication/deletion wins over any earlier directory observation.
            self.db.executemany(
                "INSERT OR REPLACE INTO media VALUES (?, ?, ?, ?, ?, ?)",
                [row for row in rows if row[0] not in self.changed],
            )
            self.db.executemany(
                "UPDATE media SET generation = ? WHERE id = ?",
                [
                    (self.generation, ident)
                    for ident in unchanged
                    if ident not in self.changed
                ],
            )

    def _reconcile(self):
        rows, unchanged = [], []
        try:
            with os.scandir(self.root) as files:
                for file in files:
                    if self.stopped.is_set():
                        return
                    ident = file.name.removesuffix(".json")
                    if not file.name.endswith(".json") or not ID.fullmatch(ident):
                        continue
                    try:
                        stat = file.stat()
                        with self.lock:
                            cached = self.db.execute(
                                "SELECT mtime, size, entry FROM media WHERE id = ?",
                                (ident,),
                            ).fetchone()
                        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                            if not (
                                self.root / json.loads(cached[2])["file"]
                            ).is_file():
                                continue
                            unchanged.append(ident)
                        else:
                            # File I/O is outside the write lock; commit small batches.
                            with open(file.path) as sidecar:
                                item = json.load(sidecar)
                            name = item.get("file")
                            if (
                                item.get("id") != ident
                                or item.get("kind") not in ("still", "video")
                                or not isinstance(name, str)
                                or os.path.basename(name) != name
                                or not (self.root / name).is_file()
                            ):
                                continue
                            rows.append(
                                (
                                    ident,
                                    item["kind"],
                                    json.dumps(item),
                                    stat.st_mtime_ns,
                                    stat.st_size,
                                    self.generation,
                                )
                            )
                        if len(rows) + len(unchanged) >= 100:
                            self._commit_import(rows, unchanged)
                            rows, unchanged = [], []
                    except (OSError, ValueError, AttributeError):
                        # Corrupt/incomplete files are retried at the next startup.
                        logging.warning("Skipped invalid library sidecar %s", file.name)
            self._commit_import(rows, unchanged)
            with self.lock, self.db:
                self.db.execute(
                    "DELETE FROM media WHERE generation != ?", (self.generation,)
                )
        except Exception:
            logging.exception("Library reconciliation failed")
            self.error = (
                "Library indexing failed. Restart to retry; originals are unchanged."
            )
        finally:
            with self.lock:
                self.indexing = False
                self.changed.clear()
            self.finished.set()

    def close(self):
        if self.closed:
            return
        self.stopped.set()
        if self.worker.ident is not None:
            self.worker.join()
        with self.lock, self.read_lock:
            self.reader.close()
            self.db.close()
            self.closed = True
