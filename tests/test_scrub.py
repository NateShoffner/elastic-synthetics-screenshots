import json
from fnmatch import fnmatchcase

import pytest

from elastic_synthetics_screenshots import cli
from elastic_synthetics_screenshots.extractor import PartialResultsError
from elastic_synthetics_screenshots.scrub import HASH_FIELD, ScrubOptions, plan_scrub, scrub

OLD, NEW = ".ds-shots-000001", ".ds-shots-000002"


def ref(index, doc_id, monitor, ts, hashes):
    source = {
        "@timestamp": ts,
        "monitor": {"id": monitor, "name": f"Monitor {monitor}", "check_group": f"cg-{doc_id}"},
        "observer": {"geo": {"name": "US East"}},
        "synthetics": {"type": "step/screenshot_ref", "step": {"index": 1, "name": "load page"}},
        "screenshot_ref": {"width": 20, "height": 10, "blocks": [{"hash": h} for h in hashes]},
    }
    return {"_index": index, "_id": doc_id, "_source": source}


def block(index, block_hash):
    return {"_index": index, "_id": block_hash, "_source": {"synthetics": {"type": "screenshot/block"}}}


def values(doc, field):
    """Every value at a dotted path, the way Elasticsearch flattens arrays."""
    if field == HASH_FIELD:
        field = "screenshot_ref.blocks.hash"
    found = [doc["_source"]]
    for part in field.split("."):
        found = [v for item in found for v in (item if isinstance(item, list) else [item])]
        found = [item[part] for item in found if isinstance(item, dict) and part in item]
    return [v for item in found for v in (item if isinstance(item, list) else [item])]


def matches(doc, query):
    (kind, body), = query.items()
    if kind == "bool":
        should = body.get("should", [])
        return (
            all(matches(doc, q) for q in body.get("filter", []))
            and not any(matches(doc, q) for q in body.get("must_not", []))
            and (not should or any(matches(doc, q) for q in should))
        )
    if kind == "ids":
        return doc["_id"] in body["values"]
    (field, wanted), = body.items()
    found = values(doc, field)
    if kind == "term":
        return wanted in found
    if kind == "terms":
        return any(v in wanted for v in found)
    if kind == "wildcard":
        return any(fnmatchcase(v, wanted) for v in found)
    if kind == "range":
        return any(wanted["gte"] <= v <= wanted["lte"] for v in found)
    raise AssertionError(f"unexpected query: {query}")


class FakeES:
    """Just enough of a data stream with two backing indices to scrub."""

    def __init__(self, docs, read_only=(), failed_shards=0):
        self.docs = list(docs)
        self.read_only = set(read_only)
        self.failed_shards = failed_shards
        self.deletes = 0

    def options(self, **_):
        return self

    def open_point_in_time(self, **_):
        return {"id": "pit"}

    def close_point_in_time(self, **_):
        pass

    def search(self, **kw):
        found = [d for d in self.docs if matches(d, kw["query"])]
        if "pit" in kw:
            found.sort(key=lambda d: d["_source"]["@timestamp"])
            start = kw["search_after"][0] if kw.get("search_after") else 0
            hits = [dict(d, sort=[start + i + 1]) for i, d in enumerate(found[start : start + kw["size"]])]
            return {"pit_id": "pit", "_shards": {"failed": self.failed_shards}, "hits": {"hits": hits}}
        assert kw["allow_partial_search_results"] is False
        if "aggs" in kw:
            assert HASH_FIELD in kw["runtime_mappings"]
            terms = kw["aggs"]["used"]["terms"]
            used = {v for d in found for v in values(d, terms["field"])} & set(terms["include"])
            return {"aggregations": {"used": {"buckets": [{"key": h} for h in sorted(used)]}}}
        return {"hits": {"hits": [{"_index": d["_index"], "_id": d["_id"]} for d in found]}}

    def bulk(self, operations, refresh):
        assert refresh is True
        items = []
        for op in operations:
            target = op["delete"]
            doc = next((d for d in self.docs if (d["_index"], d["_id"]) == (target["_index"], target["_id"])), None)
            if doc is None:
                items.append({"delete": {**target, "status": 404, "result": "not_found"}})
            elif doc["_index"] in self.read_only:
                error = {"type": "cluster_block_exception", "reason": "index is read-only"}
                items.append({"delete": {**target, "status": 403, "error": error}})
            else:
                self.docs.remove(doc)
                self.deletes += 1
                items.append({"delete": {**target, "status": 200, "result": "deleted"}})
        return {"items": items}

    def close(self):
        pass

    def ids(self):
        return sorted((d["_index"], d["_id"]) for d in self.docs)


def make_es(**kw):
    """Three runs of monitor A and one of monitor B; the 12:00 runs are the ones to scrub."""
    docs = [
        ref(OLD, "a1", "A", "2026-10-05T11:00:00.000Z", ["logo", "home"]),
        ref(NEW, "a2", "A", "2026-10-05T12:00:00.000Z", ["logo", "pii-1", "pii-2"]),
        ref(NEW, "b1", "B", "2026-10-05T12:00:00.000Z", ["logo", "other"]),
        ref(NEW, "a3", "A", "2026-10-05T13:00:00.000Z", ["logo", "home"]),
        block(OLD, "logo"),
        block(OLD, "home"),
        # Blocks are stored again after a rollover.
        block(NEW, "logo"),
        block(NEW, "home"),
        block(NEW, "pii-1"),
        block(NEW, "pii-2"),
        block(NEW, "other"),
    ]
    return FakeES(docs, **kw)


def window(**kw):
    return ScrubOptions(since="2026-10-05T11:30:00.000Z", until="2026-10-05T12:30:00.000Z", **kw)


def test_plan_selects_screenshots_and_finds_shared_blocks():
    es = make_es()
    plan = plan_scrub(es, window(monitors=["A"]))

    assert [(s["index"], s["id"], s["monitor_id"]) for s in plan.screenshots] == [(NEW, "a2", "A")]
    assert plan.screenshots[0]["check_group"] == "cg-a2"
    assert plan.blocks == {"logo", "pii-1", "pii-2"}
    assert plan.shared == {"logo"}
    assert es.deletes == 0


def test_scrub_deletes_screenshots_and_unshared_blocks():
    es = make_es()
    opts = window()
    stats = scrub(es, opts, plan_scrub(es, opts))

    assert (stats.screenshots, stats.blocks, stats.kept, stats.errors) == (2, 3, 1, [])
    assert es.ids() == [
        (OLD, "a1"),
        (OLD, "home"),
        (OLD, "logo"),
        (NEW, "a3"),
        (NEW, "home"),
        (NEW, "logo"),
    ]


def test_include_shared_deletes_every_copy_of_a_block():
    es = make_es()
    opts = window(monitors=["Monitor A"], include_shared=True)
    stats = scrub(es, opts, plan_scrub(es, opts))

    assert (stats.screenshots, stats.blocks, stats.kept) == (1, 3, 0)
    assert (OLD, "logo") not in es.ids() and (NEW, "logo") not in es.ids()
    assert (NEW, "other") in es.ids()


def test_failed_block_deletes_leave_the_screenshots_for_a_rerun():
    es = make_es(read_only=[OLD])
    opts = window(include_shared=True)
    stats = scrub(es, opts, plan_scrub(es, opts))

    assert (stats.screenshots, stats.blocks) == (0, 4)
    assert stats.errors == [f"{OLD}/logo: cluster_block_exception: index is read-only"]
    assert {(OLD, "logo"), (NEW, "a2"), (NEW, "b1")} <= set(es.ids())
    assert (NEW, "logo") not in es.ids()

    es.read_only.clear()
    stats = scrub(es, opts, plan_scrub(es, opts))
    assert (stats.screenshots, stats.blocks, stats.errors) == (2, 1, [])
    assert es.ids() == [(OLD, "a1"), (OLD, "home"), (NEW, "a3"), (NEW, "home")]


def test_block_that_came_into_use_after_planning_is_kept():
    es = make_es()
    opts = window()
    plan = plan_scrub(es, opts)
    es.docs.append(ref(NEW, "a4", "A", "2026-10-05T14:00:00.000Z", ["logo", "pii-1"]))
    stats = scrub(es, opts, plan)

    assert (stats.screenshots, stats.blocks, stats.kept) == (2, 2, 2)
    assert (NEW, "pii-1") in es.ids() and (NEW, "pii-2") not in es.ids()


def test_plan_refuses_partial_results():
    with pytest.raises(PartialResultsError):
        plan_scrub(make_es(failed_shards=1), window())


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    es = make_es()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "make_client", lambda connection: es)
    return es


WINDOW_ARGS = ["scrub", "--url", "http://es", "--from", "2026-10-05T11:30:00.000Z", "--to", "2026-10-05T12:30:00.000Z"]


def test_cli_dry_run_reports_without_deleting(fake_cli, tmp_path, capsys):
    assert cli.main(WINDOW_ARGS + ["--dry-run", "--report", "report.jsonl"]) == 0
    out = capsys.readouterr().out
    assert "selected 2 screenshot(s)" in out
    assert "image blocks: 3 to delete, 1 to keep" in out
    assert fake_cli.deletes == 0
    report = [json.loads(line) for line in (tmp_path / "report.jsonl").read_text().splitlines()]
    assert [r["id"] for r in report] == ["a2", "b1"]


def test_cli_needs_confirmation(fake_cli, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert cli.main(WINDOW_ARGS) == 1
    assert fake_cli.deletes == 0

    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert cli.main(WINDOW_ARGS) == 0
    assert "deleted 2 screenshot(s) and 3 image block(s), kept 1" in capsys.readouterr().out


def test_cli_yes_skips_the_prompt_and_failures_exit_nonzero(fake_cli, capsys):
    fake_cli.read_only.add(NEW)
    assert cli.main(WINDOW_ARGS + ["--yes"]) == 1
    assert "3 document(s) could not be deleted" in capsys.readouterr().err
    assert (NEW, "a2") in fake_cli.ids()


def test_cli_scrub_ignores_config_and_requires_a_time_range(fake_cli, tmp_path):
    (tmp_path / "config.toml").write_text('[extract]\nfrom = "now-7d"\nto = "now"\nmonitors = ["B"]\n')
    with pytest.raises(SystemExit):
        cli.main(["scrub", "--url", "http://es", "--yes"])
    assert cli.main(WINDOW_ARGS + ["--yes"]) == 0
    # Both monitors were scrubbed, not just the one named in [extract].
    assert (NEW, "a2") not in fake_cli.ids() and (NEW, "b1") not in fake_cli.ids()
