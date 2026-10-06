"""Delete synthetics screenshots from a cluster.

A screenshot is removed by deleting its ``step/screenshot_ref`` (or legacy
``step/screenshot``) document and the ``screenshot/block`` documents that hold
its image data. Blocks are shared between screenshots, so unless asked
otherwise a block is only deleted when no other screenshot points at it.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from .extractor import (
    BLOCK_TYPE,
    ID_CHUNK,
    SCREENSHOT_INDEX,
    _chunks,
    _record,
    build_query,
    iter_pages,
)

REF_TYPE = "step/screenshot_ref"
REF_SOURCE = [
    "@timestamp",
    "monitor.id",
    "monitor.name",
    "monitor.check_group",
    "observer.geo.name",
    "synthetics.step",
    "screenshot_ref.blocks.hash",
]

# The synthetics package maps the block hashes under synthetics.screenshot_ref
# with dynamic mapping off, but Heartbeat writes screenshot_ref at the document
# root, so the hashes are not indexed. Read them from _source at search time.
HASH_FIELD = "scrub_block_hash"
HASH_RUNTIME_MAPPING = {
    HASH_FIELD: {
        "type": "keyword",
        "script": {
            "source": (
                "def ref = params._source.screenshot_ref;"
                " if (ref != null && ref.blocks != null) {"
                " for (def block : ref.blocks) {"
                " if (block.hash != null) { emit(block.hash) } } }"
            )
        },
    }
}
# Each usage check runs that script over every screenshot outside the
# selection, so send few, large requests and give them time.
USAGE_CHUNK = 10000
USAGE_TIMEOUT = 600
COPIES_PAGE = 10000


@dataclass
class ScrubOptions:
    since: str
    until: str
    monitors: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    include_shared: bool = False
    page_size: int = 500
    index: str = SCREENSHOT_INDEX


@dataclass
class ScrubPlan:
    # One manifest style record per screenshot, plus its backing index and id.
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    # Every block the selected screenshots use.
    blocks: set[str] = field(default_factory=set)
    # The blocks that screenshots outside the selection use as well.
    shared: set[str] = field(default_factory=set)


@dataclass
class ScrubStats:
    screenshots: int = 0
    blocks: int = 0
    kept: int = 0
    errors: list[str] = field(default_factory=list)


def blocks_in_use(
    es: Any, index: str, hashes: list[str], exclude: dict[str, Any] | None = None
) -> set[str]:
    """Return the given blocks that a screenshot still points at.

    Screenshots matching ``exclude`` are not counted.
    """
    used: set[str] = set()
    for chunk in _chunks(hashes, USAGE_CHUNK):
        # No terms query on the hashes: on a runtime field every hash counts
        # against the cluster's clause limit. The aggregation's include list
        # does the matching instead.
        query: dict[str, Any] = {"filter": [{"term": {"synthetics.type": REF_TYPE}}]}
        if exclude:
            query["must_not"] = [exclude]
        resp = es.options(request_timeout=USAGE_TIMEOUT).search(
            index=index,
            query={"bool": query},
            runtime_mappings=HASH_RUNTIME_MAPPING,
            size=0,
            aggs={"used": {"terms": {"field": HASH_FIELD, "include": chunk, "size": len(chunk)}}},
            # A shard that fails here would make its blocks look unused.
            allow_partial_search_results=False,
        )
        used.update(bucket["key"] for bucket in resp["aggregations"]["used"]["buckets"])
    return used


def plan_scrub(es: Any, opts: ScrubOptions) -> ScrubPlan:
    """Work out what a scrub would delete, without changing anything."""
    plan = ScrubPlan()
    query = build_query(opts.monitors, opts.locations, opts.since, opts.until)
    with closing(
        iter_pages(es, opts.index, query, opts.page_size, source=REF_SOURCE, allow_partial=False)
    ) as pages:
        for hits in pages:
            for hit in hits:
                src = hit["_source"]
                plan.screenshots.append({"index": hit["_index"], "id": hit["_id"], **_record(src)})
                plan.blocks.update(
                    block["hash"] for block in src.get("screenshot_ref", {}).get("blocks", [])
                )
    plan.shared = blocks_in_use(es, opts.index, sorted(plan.blocks), exclude=query)
    return plan


def _bulk_delete(es: Any, docs: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Delete (backing index, id) pairs. Returns the ids deleted and any errors."""
    deleted: list[str] = []
    errors: list[str] = []
    for chunk in _chunks(docs, ID_CHUNK):
        resp = es.bulk(
            operations=[{"delete": {"_index": index, "_id": doc_id}} for index, doc_id in chunk],
            # Later searches have to see these deletes.
            refresh=True,
        )
        for item in resp["items"]:
            result = item["delete"]
            if result.get("result") == "deleted":
                deleted.append(result["_id"])
            elif result.get("status") != 404:
                error = result.get("error") or {}
                errors.append(
                    f"{result.get('_index')}/{result.get('_id')}:"
                    f" {error.get('type', result.get('status'))}: {error.get('reason', 'not deleted')}"
                )
    return deleted, errors


def _delete_blocks(es: Any, index: str, hashes: list[str]) -> tuple[set[str], list[str]]:
    deleted: set[str] = set()
    errors: list[str] = []
    for chunk in _chunks(hashes, ID_CHUNK):
        # A block can exist in several backing indices. Search again until
        # every copy is gone.
        while True:
            resp = es.search(
                index=index,
                query={
                    "bool": {
                        "filter": [
                            {"ids": {"values": chunk}},
                            {"term": {"synthetics.type": BLOCK_TYPE}},
                        ]
                    }
                },
                size=COPIES_PAGE,
                source=False,
                allow_partial_search_results=False,
            )
            copies = [(hit["_index"], hit["_id"]) for hit in resp["hits"]["hits"]]
            if not copies:
                break
            done, failed = _bulk_delete(es, copies)
            deleted.update(done)
            errors += failed
            if failed or not done:
                # Whatever is left cannot be deleted; do not ask for it forever.
                break
    return deleted, errors


def scrub(es: Any, opts: ScrubOptions, plan: ScrubPlan) -> ScrubStats:
    """Delete the planned screenshots and the blocks nothing else needs.

    Blocks go first. If one cannot be deleted the screenshots are left in
    place, so that running the scrub again finds the same blocks.
    """
    stats = ScrubStats()
    doomed = plan.blocks
    if not opts.include_shared:
        # Asked again rather than trusting the plan, which can be minutes old
        # by now, and against exactly the documents about to be deleted.
        selected = {"ids": {"values": [s["id"] for s in plan.screenshots]}}
        doomed = plan.blocks - blocks_in_use(es, opts.index, sorted(plan.blocks), exclude=selected)
    stats.kept = len(plan.blocks) - len(doomed)

    gone, stats.errors = _delete_blocks(es, opts.index, sorted(doomed))
    stats.blocks = len(gone)
    if not stats.errors:
        deleted, stats.errors = _bulk_delete(es, [(s["index"], s["id"]) for s in plan.screenshots])
        stats.screenshots = len(deleted)
    return stats
