"""OpenNeuro client — discover, inspect, and pull BIDS neuroimaging datasets.

This is Matilde's first scientific-data capability. It is deliberately
**read-only and stdlib-only**: it talks to the OpenNeuro GraphQL API and the
public, no-auth S3 mirror over plain HTTP — no ``datalad``, ``git-annex``,
``openneuro-py``, or AWS SDK required. Validated live against ``ds000246``.

What it does today (realistic first capability):
  - ``list_datasets`` — enumerate dataset accession IDs
  - ``get_dataset``   — metadata: name, authors, modalities, subjects, tasks, size
  - ``list_files``    — files in the latest snapshot, with direct S3 URLs
  - ``download_file`` — fetch one file to disk

What is intentionally **out of scope** (heavy / aspirational — needs system deps
and compute): cloning full datasets with DataLad/git-annex, running BIDS-App
pipelines like fMRIPrep, and large-scale meta-analysis with NiMARE. Those belong
behind the Docker image layer, not this lightweight client.

All network I/O is injected (``gql`` / ``http_get``) so every path is unit-testable
offline; production defaults using urllib are provided.
"""
from __future__ import annotations

import dataclasses
import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

OPENNEURO_GRAPHQL = "https://openneuro.org/crn/graphql"

GqlFn = Callable[..., dict]
HttpGetFn = Callable[..., bytes]


class OpenNeuroError(Exception):
    """Raised when an OpenNeuro request fails or returns nothing usable."""


@dataclass
class Dataset:
    id: str
    name: str = ""
    authors: List[str] = field(default_factory=list)
    modalities: List[str] = field(default_factory=list)
    subjects: List[str] = field(default_factory=list)
    tasks: List[str] = field(default_factory=list)
    size: Optional[int] = None
    latest_tag: str = ""
    created: str = ""
    public: Optional[bool] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

_DATASET_QUERY = """
query ($id: ID!) {
  dataset(id: $id) {
    id
    created
    public
    latestSnapshot {
      tag
      created
      size
      description { Name Authors }
      summary { subjects modalities tasks }
    }
  }
}
"""

_DATASETS_QUERY = """
query ($first: Int!) {
  datasets(first: $first) {
    edges { node { id } }
  }
}
"""

_SNAPSHOT_FILES_QUERY = """
query ($datasetId: ID!, $tag: String!) {
  snapshot(datasetId: $datasetId, tag: $tag) {
    id
    files { filename size urls }
  }
}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_dataset(dataset_id: str, gql: Optional[GqlFn] = None) -> Dataset:
    """Return metadata for *dataset_id* (e.g. ``"ds000246"``)."""
    gql = gql or default_gql
    data = gql(_DATASET_QUERY, {"id": dataset_id})
    node = (data or {}).get("dataset")
    if not node:
        raise OpenNeuroError(f"OpenNeuro dataset {dataset_id!r} not found.")
    snap = node.get("latestSnapshot") or {}
    desc = snap.get("description") or {}
    summary = snap.get("summary") or {}
    return Dataset(
        id=node.get("id", dataset_id),
        name=desc.get("Name", "") or "",
        authors=list(desc.get("Authors") or []),
        modalities=list(summary.get("modalities") or []),
        subjects=list(summary.get("subjects") or []),
        tasks=list(summary.get("tasks") or []),
        size=snap.get("size"),
        latest_tag=snap.get("tag", "") or "",
        created=node.get("created", "") or "",
        public=node.get("public"),
    )


def list_datasets(limit: int = 20, gql: Optional[GqlFn] = None) -> List[str]:
    """Return up to *limit* OpenNeuro dataset accession IDs.

    Note: this lists datasets (most recent first as OpenNeuro orders them); it is
    not a full-text search. Use ``get_dataset`` to inspect each candidate's
    modalities/tasks and filter client-side.
    """
    gql = gql or default_gql
    data = gql(_DATASETS_QUERY, {"first": int(limit)})
    edges = ((data or {}).get("datasets") or {}).get("edges") or []
    return [e["node"]["id"] for e in edges if e.get("node", {}).get("id")]


def list_files(dataset_id: str, tag: Optional[str] = None,
               gql: Optional[GqlFn] = None) -> List[dict]:
    """Return the files in a dataset snapshot: ``[{filename, size, url}, ...]``.

    If *tag* is omitted, the latest snapshot tag is resolved automatically.
    """
    gql = gql or default_gql
    if not tag:
        tag = get_dataset(dataset_id, gql=gql).latest_tag
        if not tag:
            raise OpenNeuroError(f"No snapshot tag available for {dataset_id!r}.")
    data = gql(_SNAPSHOT_FILES_QUERY, {"datasetId": dataset_id, "tag": tag})
    snap = (data or {}).get("snapshot")
    if not snap:
        raise OpenNeuroError(f"Snapshot {dataset_id}:{tag} not found.")
    out = []
    for f in snap.get("files") or []:
        urls = f.get("urls") or []
        out.append({"filename": f.get("filename", ""), "size": f.get("size"),
                    "url": urls[0] if urls else ""})
    return out


def download_file(dataset_id: str, filename: str, dest_path: str,
                  tag: Optional[str] = None, gql: Optional[GqlFn] = None,
                  http_get: Optional[HttpGetFn] = None) -> str:
    """Download a single file from a dataset snapshot to *dest_path*.

    Returns the path written. Raises ``OpenNeuroError`` if the file is not in the
    snapshot. Intended for small files (metadata, README, single NIfTI); pulling
    whole datasets is out of scope for this lightweight client — use DataLad.
    """
    gql = gql or default_gql
    http_get = http_get or default_http_get
    files = list_files(dataset_id, tag=tag, gql=gql)
    match = next((f for f in files if f["filename"] == filename), None)
    if match is None:
        raise OpenNeuroError(
            f"{filename!r} not found in {dataset_id} (snapshot has {len(files)} files).")
    if not match["url"]:
        raise OpenNeuroError(f"No download URL for {filename!r}.")
    content = http_get(match["url"])
    parent = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(parent, exist_ok=True)
    with open(dest_path, "wb") as fh:
        fh.write(content)
    return dest_path


# ---------------------------------------------------------------------------
# Production I/O defaults (stdlib only)
# ---------------------------------------------------------------------------

def _user_agent() -> str:
    contact = os.environ.get("MATILDE_CONTACT_EMAIL", "").strip()
    base = "Matilde/0.1 (https://github.com/cyborg-garden/Matilde)"
    return f"{base} mailto:{contact}" if contact else base


def default_gql(query: str, variables: Optional[dict] = None,
                timeout: float = 30.0) -> dict:
    """POST a GraphQL query to OpenNeuro and return the ``data`` object."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        OPENNEURO_GRAPHQL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": _user_agent()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("errors"):
        raise OpenNeuroError(f"GraphQL error: {payload['errors']}")
    return payload.get("data") or {}


def default_http_get(url: str, timeout: float = 120.0) -> bytes:
    """GET *url* and return raw bytes (for S3 file downloads)."""
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
