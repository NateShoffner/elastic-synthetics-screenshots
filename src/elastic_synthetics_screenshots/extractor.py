"""Query synthetics screenshot documents and write them to disk.

Browser monitors store screenshots in two shapes:

* ``step/screenshot_ref``: a layout (``screenshot_ref.blocks``) pointing at
  deduplicated ``screenshot/block`` documents whose ``_id`` is the block hash.
  The full image has to be stitched together from those blocks.
* ``step/screenshot``: the legacy shape, with the whole image base64 encoded in
  ``synthetics.blob``.
"""

from __future__ import annotations

import base64
import json
import re
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .compose import compose

SCREENSHOT_INDEX = "synthetics-browser.screenshot-*"
STATUS_INDEX = "synthetics-browser-*"

SCREENSHOT_TYPES = ["step/screenshot_ref", "step/screenshot"]
BLOCK_TYPE = "screenshot/block"
MONITOR_FIELDS = ("monitor.id", "monitor.name", "config_id")
MIME_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png"}

PIT_KEEP_ALIVE = "2m"
BLOCK_CACHE_SIZE = 4096
ID_CHUNK = 500
CHECK_GROUP_CHUNK = 50


@dataclass
class Options:
    out: Path = Path("screenshots")
    monitors: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    since: str = "now-24h"
    until: str = "now"
    status: str | None = None
    limit: int | None = None
    image_format: str = "jpg"
    overwrite: bool = False
    page_size: int = 100
    index: str = SCREENSHOT_INDEX
    status_index: str = STATUS_INDEX


@dataclass
class Stats:
    written: int = 0
    existing: int = 0
    filtered: int = 0
    incomplete: int = 0


def build_query(
    monitors: list[str], locations: list[str], since: str, until: str
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = [
        {"terms": {"synthetics.type": SCREENSHOT_TYPES}},
        {"range": {"@timestamp": {"gte": since, "lte": until}}},
    ]
    if monitors:
        should = []
        for monitor in monitors:
            kind = "wildcard" if any(c in monitor for c in "*?") else "term"
            should += [{kind: {f: monitor}} for f in MONITOR_FIELDS]
        filters.append({"bool": {"should": should, "minimum_should_match": 1}})
    if locations:
        filters.append({"terms": {"observer.geo.name": locations}})
    return {"bool": {"filter": filters}}


def slugify(value: str, max_length: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return slug[:max_length] or "unnamed"


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class PartialResultsError(Exception):
    """A search came back without results from every shard."""


def iter_pages(
    es: Any,
    index: str,
    query: dict[str, Any],
    page_size: int,
    source: list[str] | None = None,
    allow_partial: bool = True,
) -> Iterator[list[dict[str, Any]]]:
    """Page through screenshot documents, oldest first."""
    pit = es.open_point_in_time(index=index, keep_alive=PIT_KEEP_ALIVE)["id"]
    try:
        search_after = None
        while True:
            resp = es.search(
                pit={"id": pit, "keep_alive": PIT_KEEP_ALIVE},
                query=query,
                sort=[{"@timestamp": "asc"}, {"_shard_doc": "asc"}],
                size=page_size,
                search_after=search_after,
                track_total_hits=False,
                source=source,
            )
            pit = resp.get("pit_id", pit)
            failed = resp.get("_shards", {}).get("failed", 0)
            if failed and not allow_partial:
                raise PartialResultsError(f"{failed} shard(s) failed while listing screenshots in {index}")
            hits = resp["hits"]["hits"]
            if not hits:
                return
            yield hits
            search_after = hits[-1]["sort"]
    finally:
        es.close_point_in_time(id=pit)


def fetch_blocks(es: Any, index: str, hashes: list[str]) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    for chunk in _chunks(hashes, ID_CHUNK):
        # A block can exist in several backing indices, and the duplicates can
        # crowd other ids out of a response. Ask again for whatever is left.
        remaining = set(chunk)
        while remaining:
            resp = es.search(
                index=index,
                query={
                    "bool": {
                        "filter": [
                            {"ids": {"values": sorted(remaining)}},
                            {"term": {"synthetics.type": BLOCK_TYPE}},
                        ]
                    }
                },
                size=len(remaining),
                source=["synthetics.blob"],
            )
            found = set()
            for hit in resp["hits"]["hits"]:
                blob = hit["_source"].get("synthetics", {}).get("blob")
                if blob and hit["_id"] in remaining:
                    blobs[hit["_id"]] = base64.b64decode(blob)
                    found.add(hit["_id"])
            if not found:
                break
            remaining -= found
    return blobs


def fetch_statuses(
    es: Any, index: str, check_groups: list[str]
) -> dict[tuple[str, int], str]:
    """Map (check_group, step index) to the step status from step/end events."""
    statuses: dict[tuple[str, int], str] = {}
    for chunk in _chunks(check_groups, CHECK_GROUP_CHUNK):
        resp = es.search(
            index=index,
            query={
                "bool": {
                    "filter": [
                        {"terms": {"monitor.check_group": chunk}},
                        {"term": {"synthetics.type": "step/end"}},
                    ]
                }
            },
            size=10000,
            source=["monitor.check_group", "synthetics.step.index", "synthetics.step.status"],
            ignore_unavailable=True,
        )
        for hit in resp["hits"]["hits"]:
            src = hit["_source"]
            step = src.get("synthetics", {}).get("step", {})
            if "index" in step and "status" in step:
                statuses[(src["monitor"]["check_group"], step["index"])] = step["status"]
    return statuses


def _record(src: dict[str, Any]) -> dict[str, Any]:
    monitor = src.get("monitor", {})
    step = src.get("synthetics", {}).get("step", {})
    return {
        "timestamp": src.get("@timestamp"),
        "monitor_id": monitor.get("id"),
        "monitor_name": monitor.get("name"),
        "check_group": monitor.get("check_group"),
        "location": src.get("observer", {}).get("geo", {}).get("name"),
        "step_index": step.get("index"),
        "step_name": step.get("name"),
    }


def _target_path(out: Path, record: dict[str, Any], extension: str) -> Path:
    monitor = slugify(record["monitor_name"] or record["monitor_id"] or "unknown")
    try:
        ts = datetime.fromisoformat(record["timestamp"]).astimezone(timezone.utc)
        stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    except (TypeError, ValueError):
        stamp = "unknown-time"
    run = slugify(record["check_group"] or "unknown")[:8]
    name = f"{stamp}_{run}_step{record['step_index'] or 0:02d}"
    if record["step_name"]:
        name += f"_{slugify(record['step_name'], 60)}"
    return out / monitor / f"{name}.{extension}"


def extract(
    es: Any, opts: Options, log: Callable[[str], None] = lambda _: None
) -> Stats:
    stats = Stats()
    # Blocks are shared heavily between runs; keep the most recent ones around.
    cache: dict[str, bytes] = {}
    query = build_query(opts.monitors, opts.locations, opts.since, opts.until)
    opts.out.mkdir(parents=True, exist_ok=True)

    with (
        open(opts.out / "manifest.jsonl", "a", encoding="utf-8") as manifest,
        closing(iter_pages(es, opts.index, query, opts.page_size)) as pages,
    ):
        for hits in pages:
            check_groups = sorted(
                {cg for h in hits if (cg := h["_source"].get("monitor", {}).get("check_group"))}
            )
            statuses = fetch_statuses(es, opts.status_index, check_groups)

            # Decide what to write before fetching any block data.
            todo: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
            for hit in hits:
                if opts.limit is not None and stats.written + len(todo) >= opts.limit:
                    break
                src = hit["_source"]
                record = _record(src)
                record["step_status"] = statuses.get((record["check_group"], record["step_index"]))
                if opts.status and record["step_status"] != opts.status:
                    stats.filtered += 1
                    continue
                if "screenshot_ref" in src:
                    extension = opts.image_format
                else:
                    extension = MIME_EXTENSIONS.get(src["synthetics"].get("blob_mime"), "jpg")
                path = _target_path(opts.out, record, extension)
                if path.exists() and not opts.overwrite:
                    stats.existing += 1
                    continue
                todo.append((src, record, path))

            needed = {
                block["hash"]
                for src, _, _ in todo
                for block in src.get("screenshot_ref", {}).get("blocks", [])
            }
            blobs = {h: cache[h] for h in needed if h in cache}
            blobs.update(fetch_blocks(es, opts.index, sorted(needed - blobs.keys())))
            cache.update(blobs)
            while len(cache) > BLOCK_CACHE_SIZE:
                del cache[next(iter(cache))]

            for src, record, path in todo:
                path.parent.mkdir(parents=True, exist_ok=True)
                ref = src.get("screenshot_ref")
                if ref is None:
                    path.write_bytes(base64.b64decode(src["synthetics"]["blob"]))
                else:
                    image, missing = compose(ref["width"], ref["height"], ref["blocks"], blobs)
                    if path.suffix == ".png":
                        image.save(path, "PNG")
                    else:
                        image.save(path, "JPEG", quality=95)
                    record["width"] = ref["width"]
                    record["height"] = ref["height"]
                    if missing:
                        record["missing_blocks"] = missing
                        stats.incomplete += 1
                        log(f"warning: {missing} block(s) missing for {path}")
                record["path"] = path.relative_to(opts.out).as_posix()
                manifest.write(json.dumps(record) + "\n")
                stats.written += 1
                log(str(path))

            if opts.limit is not None and stats.written >= opts.limit:
                break
    return stats


def list_monitors(es: Any, index: str, since: str, until: str) -> list[dict[str, Any]]:
    """Summarize which monitors have screenshots in the time range."""
    monitors: list[dict[str, Any]] = []
    after = None
    while True:
        composite: dict[str, Any] = {
            "size": 500,
            "sources": [
                {"id": {"terms": {"field": "monitor.id"}}},
                {"name": {"terms": {"field": "monitor.name", "missing_bucket": True}}},
            ],
        }
        if after:
            composite["after"] = after
        resp = es.search(
            index=index,
            query=build_query([], [], since, until),
            size=0,
            aggs={
                "monitors": {
                    "composite": composite,
                    "aggs": {"latest": {"max": {"field": "@timestamp"}}},
                }
            },
        )
        agg = resp.get("aggregations", {}).get("monitors", {})
        for bucket in agg.get("buckets", []):
            monitors.append(
                {
                    "id": bucket["key"]["id"],
                    "name": bucket["key"]["name"],
                    "screenshots": bucket["doc_count"],
                    "latest": bucket["latest"].get("value_as_string"),
                }
            )
        after = agg.get("after_key")
        if not after:
            return monitors
