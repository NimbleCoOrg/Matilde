"""StudyStore — durable, checkpointed analysis state for Matilde studies.

A *study* is a long-running analysis (e.g. validating a paper against a dataset)
decomposed into ordered *steps*. Each step can emit *artifacts* (references to
on-disk files — never blobs in the DB) and *findings* (the scientific output:
claim + verdict + evidence). All of it is persisted to a SQLite file so the work

  - **survives an OOM / mid-step failure** — a failed step leaves the study
    resumable; completed steps are never lost, and
  - **survives a container rebuild** — the db lives on the mounted data volume,
    so a brand-new ``StudyStore`` pointing at the same file sees all prior state.

Design mirrors the rest of ``matilde_plugin/engine``: stdlib-only (``sqlite3``),
pure data in / data out, injected db path. WAL mode + autocommit make each write
atomic and crash-safe.

The db path is injected. ``default_db_path()`` puts it under the engagements data
dir (``MATILDE_STUDY_DB`` override, else ``$HERMES_HOME``/engagements, else the
repo's ``engagements/`` dir) — the same on-volume location the rest of Matilde's
private operational data uses.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'created',
    plan_json   TEXT NOT NULL DEFAULT '[]',
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    INTEGER NOT NULL REFERENCES studies(id),
    idx         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT,
    error       TEXT,
    started_at  TEXT,
    finished_at TEXT,
    UNIQUE(study_id, name)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    INTEGER NOT NULL REFERENCES studies(id),
    step_name   TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    sha256      TEXT NOT NULL DEFAULT '',
    bytes       INTEGER,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id      INTEGER NOT NULL REFERENCES studies(id),
    step_name     TEXT NOT NULL DEFAULT '',
    claim         TEXT NOT NULL DEFAULT '',
    verdict       TEXT NOT NULL DEFAULT '',
    score         REAL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_db_path() -> str:
    """Resolve the on-volume db path the rest of Matilde's private data uses.

    Precedence: ``MATILDE_STUDY_DB`` (explicit override) → ``$HERMES_HOME``/
    engagements → the repo's ``engagements/`` dir. The directory is created if
    needed; this keeps study state on the mounted data volume so it survives a
    container rebuild.
    """
    override = os.environ.get("MATILDE_STUDY_DB", "").strip()
    if override:
        return override
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        base = os.path.join(home, "engagements")
    else:
        # Repo-relative engagements/ (gitignored) — the default private data dir.
        engine_dir = os.path.dirname(os.path.realpath(__file__))
        repo_root = os.path.normpath(os.path.join(engine_dir, "..", ".."))
        base = os.path.join(repo_root, "engagements")
    return os.path.join(base, "studies.db")


class StudyStore:
    """SQLite-backed store for studies, steps, artifacts, and findings.

    Open many instances against the same db file — each opens its own connection;
    WAL mode keeps reads/writes consistent. Every mutating method commits
    immediately (atomic), so state is durable the instant a method returns.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or default_db_path()
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def transaction(self):
        """Group several writes into one all-or-nothing unit.

        The connection runs with ``isolation_level=None`` (autocommit), which makes
        every individual write durable the instant it returns — but also means a
        sequence of writes has *no* atomicity unless the transaction is opened
        explicitly. ``BEGIN IMMEDIATE`` takes the write lock up front, so a crash
        part-way through rolls the whole group back rather than leaving half of it
        committed.

        Used by the pipeline runner to persist a step's findings and flip its status
        together: separately, a crash between the two re-ran a "pending" step that
        had already written its findings, duplicating them on resume.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ---- studies ---------------------------------------------------------

    def create_study(self, slug: str, title: str = "",
                     plan: Optional[List[str]] = None,
                     meta: Optional[dict] = None) -> int:
        """Create (or return the existing) study for *slug*. Idempotent on slug."""
        plan = list(plan or [])
        existing = self._conn.execute(
            "SELECT id FROM studies WHERE slug = ?", (slug,)).fetchone()
        if existing is not None:
            return int(existing["id"])
        now = _now()
        cur = self._conn.execute(
            "INSERT INTO studies (slug, title, status, plan_json, meta_json, "
            "created_at, updated_at) VALUES (?, ?, 'created', ?, ?, ?, ?)",
            (slug, title, json.dumps(plan), json.dumps(meta or {}), now, now),
        )
        return int(cur.lastrowid)

    def get_study(self, study_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM studies WHERE id = ?", (study_id,)).fetchone()
        return self._study_row(row) if row else None

    def get_study_by_slug(self, slug: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM studies WHERE slug = ?", (slug,)).fetchone()
        return self._study_row(row) if row else None

    def list_studies(self, limit: int = 50) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM studies ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [self._study_row(r) for r in rows]

    def set_study_status(self, study_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE studies SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), study_id))

    @staticmethod
    def _study_row(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "slug": row["slug"],
            "title": row["title"],
            "status": row["status"],
            "plan": json.loads(row["plan_json"] or "[]"),
            "meta": json.loads(row["meta_json"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---- steps -----------------------------------------------------------

    def add_steps(self, study_id: int, names: List[str]) -> None:
        """Create pending steps in order. Idempotent: existing steps keep their
        status; new names are appended after the current max index."""
        existing = {r["name"]: r["idx"] for r in self._conn.execute(
            "SELECT name, idx FROM steps WHERE study_id = ?", (study_id,))}
        next_idx = (max(existing.values()) + 1) if existing else 0
        for name in names:
            if name in existing:
                continue
            self._conn.execute(
                "INSERT INTO steps (study_id, idx, name, status) "
                "VALUES (?, ?, ?, 'pending')",
                (study_id, next_idx, name))
            next_idx += 1

    def get_steps(self, study_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE study_id = ? ORDER BY idx ASC", (study_id,)
        ).fetchall()
        return [self._step_row(r) for r in rows]

    def get_step(self, study_id: int, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM steps WHERE study_id = ? AND name = ?",
            (study_id, name)).fetchone()
        return self._step_row(row) if row else None

    def set_step_status(self, study_id: int, name: str, status: str,
                        error: Optional[str] = None) -> None:
        now = _now()
        sets = ["status = ?"]
        params: list = [status]
        if status == "running":
            sets.append("started_at = ?")
            params.append(now)
        if status in ("done", "failed", "skipped"):
            sets.append("finished_at = ?")
            params.append(now)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        params += [study_id, name]
        self._conn.execute(
            f"UPDATE steps SET {', '.join(sets)} WHERE study_id = ? AND name = ?",
            params)

    def record_step_result(self, study_id: int, name: str, result: dict) -> None:
        self._conn.execute(
            "UPDATE steps SET result_json = ? WHERE study_id = ? AND name = ?",
            (json.dumps(result, default=str), study_id, name))

    @staticmethod
    def _step_row(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "study_id": int(row["study_id"]),
            "idx": int(row["idx"]),
            "name": row["name"],
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    # ---- artifacts -------------------------------------------------------

    def add_artifact(self, study_id: int, step_name: str, path: str,
                     kind: str = "", sha256: str = "",
                     bytes: Optional[int] = None,  # noqa: A002 (mirror schema)
                     meta: Optional[dict] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO artifacts (study_id, step_name, path, kind, sha256, "
            "bytes, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (study_id, step_name, path, kind, sha256, bytes,
             json.dumps(meta or {}), _now()))
        return int(cur.lastrowid)

    def get_artifacts(self, study_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE study_id = ? ORDER BY id ASC",
            (study_id,)).fetchall()
        return [{
            "id": int(r["id"]),
            "step_name": r["step_name"],
            "path": r["path"],
            "kind": r["kind"],
            "sha256": r["sha256"],
            "bytes": r["bytes"],
            "meta": json.loads(r["meta_json"] or "{}"),
            "created_at": r["created_at"],
        } for r in rows]

    # ---- findings --------------------------------------------------------

    def add_finding(self, study_id: int, step_name: str, claim: str,
                    verdict: str, score: Optional[float] = None,
                    evidence: Optional[dict] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO findings (study_id, step_name, claim, verdict, score, "
            "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (study_id, step_name, claim, verdict, score,
             json.dumps(evidence or {}), _now()))
        return int(cur.lastrowid)

    def get_findings(self, study_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM findings WHERE study_id = ? ORDER BY id ASC",
            (study_id,)).fetchall()
        return [{
            "id": int(r["id"]),
            "step_name": r["step_name"],
            "claim": r["claim"],
            "verdict": r["verdict"],
            "score": r["score"],
            "evidence": json.loads(r["evidence_json"] or "{}"),
            "created_at": r["created_at"],
        } for r in rows]

    # ---- summary ---------------------------------------------------------

    def study_summary(self, study_id: int) -> Optional[dict]:
        study = self.get_study(study_id)
        if study is None:
            return None
        steps = self.get_steps(study_id)
        findings = self.get_findings(study_id)
        counts: dict = {}
        for s in steps:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        return {
            "study": study,
            "steps": steps,
            "step_status_counts": counts,
            "artifacts": self.get_artifacts(study_id),
            "findings": findings,
            "finding_count": len(findings),
        }

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __del__(self):  # best-effort cleanup on GC (simulated restart in tests)
        self.close()
