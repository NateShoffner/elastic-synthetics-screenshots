import base64
import json
from io import BytesIO

from PIL import Image

from elastic_synthetics_screenshots.extractor import Options, build_query, extract, slugify


def tile(color, fmt="PNG"):
    buf = BytesIO()
    Image.new("RGB", (10, 10), color).save(buf, fmt)
    return buf.getvalue()


RED, BLUE = (255, 0, 0), (0, 0, 255)


def ref_hit(check_group, step_index, ts, blocks):
    return {
        "_id": f"{check_group}-{step_index}",
        "_source": {
            "@timestamp": ts,
            "monitor": {"id": "mon-1", "name": "Checkout / flow", "check_group": check_group},
            "observer": {"geo": {"name": "US East"}},
            "synthetics": {
                "type": "step/screenshot_ref",
                "step": {"index": step_index, "name": "load page"},
            },
            "screenshot_ref": {"width": 20, "height": 10, "blocks": blocks},
        },
    }


BLOCKS = [
    {"hash": "red", "top": 0, "left": 0, "width": 10, "height": 10},
    {"hash": "blue", "top": 0, "left": 10, "width": 10, "height": 10},
]


class FakeES:
    def __init__(self, refs, blocks, statuses):
        self.refs, self.blocks, self.statuses = refs, blocks, statuses
        self.pit_closed = False
        self.block_requests = 0

    def open_point_in_time(self, **_):
        return {"id": "pit"}

    def close_point_in_time(self, **_):
        self.pit_closed = True

    def search(self, **kw):
        if "pit" in kw:
            start = kw["search_after"][0] if kw.get("search_after") else 0
            page = self.refs[start : start + kw["size"]]
            hits = [dict(h, sort=[start + i + 1]) for i, h in enumerate(page)]
            return {"pit_id": "pit", "hits": {"hits": hits}}
        filters = kw["query"]["bool"]["filter"]
        if "ids" in filters[0]:
            self.block_requests += 1
            assert filters[1] == {"term": {"synthetics.type": "screenshot/block"}}
            hits = [
                {"_id": h, "_source": {"synthetics": {"blob": base64.b64encode(self.blocks[h]).decode()}}}
                for h in filters[0]["ids"]["values"]
                if h in self.blocks
            ]
            return {"hits": {"hits": hits}}
        hits = [
            {
                "_source": {
                    "monitor": {"check_group": cg},
                    "synthetics": {"step": {"index": idx, "status": status}},
                }
            }
            for (cg, idx), status in self.statuses.items()
            if cg in filters[0]["terms"]["monitor.check_group"]
        ]
        return {"hits": {"hits": hits}}


def make_es():
    refs = [
        ref_hit("aaaaaaaa-1111", 1, "2026-10-05T12:00:00.000Z", BLOCKS),
        ref_hit("bbbbbbbb-2222", 1, "2026-10-05T12:10:00.000Z", BLOCKS),
    ]
    statuses = {("aaaaaaaa-1111", 1): "succeeded", ("bbbbbbbb-2222", 1): "failed"}
    return FakeES(refs, {"red": tile(RED), "blue": tile(BLUE)}, statuses)


def test_extract_stitches_blocks_and_writes_manifest(tmp_path):
    es = make_es()
    stats = extract(es, Options(out=tmp_path, image_format="png", page_size=1))

    assert stats.written == 2
    assert es.pit_closed
    # Blocks are shared between both screenshots, so the second page hits the cache.
    assert es.block_requests == 1

    path = tmp_path / "Checkout-flow" / "20261005T120000Z_aaaaaaaa_step01_load-page.png"
    with Image.open(path) as image:
        assert image.size == (20, 10)
        assert image.getpixel((5, 5)) == RED
        assert image.getpixel((15, 5)) == BLUE

    records = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text().splitlines()]
    assert [r["step_status"] for r in records] == ["succeeded", "failed"]
    assert records[0]["location"] == "US East"
    assert records[0]["path"] == path.relative_to(tmp_path).as_posix()


def test_extract_skips_existing_and_filters_status(tmp_path):
    stats = extract(make_es(), Options(out=tmp_path, status="failed"))
    assert (stats.written, stats.filtered) == (1, 1)

    stats = extract(make_es(), Options(out=tmp_path))
    assert (stats.written, stats.existing) == (1, 1)


def test_extract_respects_limit(tmp_path):
    stats = extract(make_es(), Options(out=tmp_path, limit=1))
    assert stats.written == 1


def test_missing_blocks_are_reported(tmp_path):
    es = make_es()
    del es.blocks["blue"]
    stats = extract(es, Options(out=tmp_path, limit=1))
    assert stats.incomplete == 1
    record = json.loads((tmp_path / "manifest.jsonl").read_text().splitlines()[0])
    assert record["missing_blocks"] == 1


def test_legacy_screenshot_is_written_as_is(tmp_path):
    blob = tile(RED, "JPEG")
    hit = ref_hit("cccccccc-3333", 2, "2026-10-05T13:00:00.000Z", [])
    del hit["_source"]["screenshot_ref"]
    hit["_source"]["synthetics"].update(
        type="step/screenshot", blob=base64.b64encode(blob).decode(), blob_mime="image/jpeg"
    )
    extract(FakeES([hit], {}, {}), Options(out=tmp_path, image_format="png"))
    path = tmp_path / "Checkout-flow" / "20261005T130000Z_cccccccc_step02_load-page.jpg"
    assert path.read_bytes() == blob


def test_build_query_uses_wildcards_only_when_asked():
    query = build_query(["checkout*", "mon-1"], ["US East"], "now-1h", "now")
    monitor_filter = query["bool"]["filter"][2]["bool"]["should"]
    assert {"wildcard": {"monitor.name": "checkout*"}} in monitor_filter
    assert {"term": {"monitor.id": "mon-1"}} in monitor_filter
    assert query["bool"]["filter"][3] == {"terms": {"observer.geo.name": ["US East"]}}


def test_slugify():
    assert slugify('a/b\\c: "d"?') == "a-b-c-d"
    assert slugify("///") == "unnamed"
