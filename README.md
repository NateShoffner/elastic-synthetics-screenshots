# elastic-synthetics-screenshots

[![CI](https://github.com/nateshoffner/elastic-synthetics-screenshots/actions/workflows/ci.yml/badge.svg)](https://github.com/nateshoffner/elastic-synthetics-screenshots/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Works with the screenshots that Elastic Synthetics browser monitors store in an Elasticsearch cluster:

- **extract** pulls them out of the cluster and writes them to disk as image files.
- **scrub** permanently deletes the ones in a time range from the cluster, for when a screenshot captured something it should not have, such as PII.

Synthetics stores each screenshot as a layout document plus deduplicated image blocks in the `synthetics-browser.screenshot-*` data stream. Extracting reads the layouts, fetches the blocks, and stitches the full images back together. Scrubbing deletes the layouts and the blocks that only they used.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```
uv sync
```

## Configuration

Settings come from two files, both git ignored, each with a committed example:

- `config.toml` holds the cluster URL, time range, monitors, and output options. Copy `config.example.toml` to `config.toml`; it documents every key. Credentials may go here too.
- `.env` is the place for credentials if you prefer to keep them separate. Copy `.env.example` to `.env` and set `ES_API_KEY`, or `ES_USERNAME` and `ES_PASSWORD`.

Both are loaded from the current directory automatically. Point at other files with `--config` / `ES_CONFIG_FILE` and `--env-file` / `ES_ENV_FILE`.

```toml
[connection]
url = "https://your-cluster.example.com:9243"

[extract]
from = "now-7d"
monitors = ["Checkout flow", "login*"]
status = "failed"
```

Every connection setting also has a flag and an environment variable:

| Flag | Environment variable | Config key |
| --- | --- | --- |
| `--url` | `ES_URL` | `connection.url` |
| `--cloud-id` | `ES_CLOUD_ID` | `connection.cloud_id` |
| `--ca-certs` | `ES_CA_CERTS` | `connection.ca_certs` |
| `--insecure` | | `connection.insecure` |
| `--api-key` | `ES_API_KEY` | `connection.api_key` |
| `--username` / `--password` | `ES_USERNAME` / `ES_PASSWORD` | `connection.username` / `connection.password` |

Precedence, highest first: command line flags, environment variables (including `.env`), the config file, built-in defaults. See [Permissions](#permissions) for what the credentials need to be allowed to do.

The `[extract]` table only applies to `list` and `extract`. `scrub` reads the connection settings but takes what to delete from its flags alone.

## Permissions

`list` and `extract` only run searches and point-in-time reads, so the credentials need the `read` index privilege on two data stream patterns and no cluster privileges:

| Pattern | Used for |
| --- | --- |
| `synthetics-browser.screenshot-*` | screenshot layouts and image blocks |
| `synthetics-browser-*` | step results, to record and filter by step status |

If you pass a custom `--index` or `--status-index`, grant `read` on those instead.

The simplest option is an API key scoped to exactly that, created by a user who can manage API keys:

```
POST /_security/api_key
{
  "name": "elastic-synthetics-screenshots",
  "role_descriptors": {
    "synthetics_screenshot_reader": {
      "indices": [
        {
          "names": ["synthetics-browser.screenshot-*", "synthetics-browser-*"],
          "privileges": ["read"]
        }
      ]
    }
  }
}
```

Put the `encoded` value from the response in `.env` as `ES_API_KEY`.

If you would rather use a role and a user or long-lived key, the equivalent role is:

```
PUT /_security/role/synthetics_screenshot_reader
{
  "indices": [
    {
      "names": ["synthetics-browser.screenshot-*", "synthetics-browser-*"],
      "privileges": ["read"]
    }
  ]
}
```

For a cross-cluster search setup, grant the same `read` privilege on the remote cluster and pass the remote pattern, for example `--index "remote:synthetics-browser.screenshot-*"`.

`scrub` also needs the `delete` index privilege on `synthetics-browser.screenshot-*`, and does not use `synthetics-browser-*`. Consider a separate, short-lived key for it rather than adding `delete` to the key you extract with:

```
POST /_security/api_key
{
  "name": "elastic-synthetics-screenshots-scrub",
  "expiration": "1d",
  "role_descriptors": {
    "synthetics_screenshot_scrubber": {
      "indices": [
        {
          "names": ["synthetics-browser.screenshot-*"],
          "privileges": ["read", "delete"]
        }
      ]
    }
  }
}
```

Scrubbing has to run against the cluster that holds the data. It cannot delete through a cross-cluster search pattern.

## Usage

See which monitors have screenshots:

```
uv run elastic-synthetics-screenshots list --from now-7d
```

Extract screenshots:

```
# everything from the last 24 hours
uv run elastic-synthetics-screenshots extract

# one monitor, last 7 days, failed steps only
uv run elastic-synthetics-screenshots extract -m "Checkout flow" --from now-7d --status failed

# wildcard match, specific location, lossless output
uv run elastic-synthetics-screenshots extract -m "checkout*" -l "US East" --format png -o ./out
```

`--monitor` matches the monitor id, name, or config id. `--from` and `--to` accept Elasticsearch date math (`now-7d`) or ISO 8601 timestamps. Run `uv run elastic-synthetics-screenshots extract --help` for all options.

## Scrubbing

`scrub` permanently deletes screenshots from the cluster. It always needs an explicit `--from` and `--to`, and can be narrowed with `--monitor` and `--location` the same way as `extract`.

```
# see what would be deleted, and keep a record of it
uv run elastic-synthetics-screenshots scrub --from 2026-10-05T12:00:00Z --to 2026-10-05T12:30:00Z -m "Checkout flow" --dry-run --report scrubbed.jsonl

# delete, after typing "yes" at the prompt
uv run elastic-synthetics-screenshots scrub --from 2026-10-05T12:00:00Z --to 2026-10-05T12:30:00Z -m "Checkout flow"
```

```
selected 12 screenshot(s), 2026-10-05T12:00:07.412Z to 2026-10-05T12:20:09.118Z:
      12  checkout-flow-default  Checkout flow
image blocks: 97 to delete, 671 to keep because other screenshots use them
Permanently delete these from https://your-cluster.example.com:9243? Type "yes" to continue:
```

To look at the screenshots first, run `extract` with the same `--from`, `--to`, `-m`, and `-l`. Keep in mind that `extract` also applies the `[extract]` table of the config file and `scrub` does not, so a `monitors` or `status` setting there narrows what you review but not what gets deleted. The summary printed before the prompt is what will be deleted.

| Flag | Effect |
| --- | --- |
| `--dry-run` | print the summary and stop |
| `-y`, `--yes` | skip the confirmation prompt, for scripts |
| `--report FILE` | write one JSON line per selected screenshot (monitor, check group, step, timestamp, document id) |
| `--include-shared-blocks` | also delete image blocks that other screenshots use |

### What gets deleted

For every selected screenshot, its layout document (or the whole document, for the older format that embeds the image) and each image block that no remaining screenshot uses, in every backing index that holds a copy.

Blocks that screenshots outside the selection also use are kept by default, because deleting them would leave holes in those screenshots. Those blocks show something that is also visible in a screenshot you chose to keep, so if one of them contains what you are scrubbing, widen the time range to cover every screenshot that shows it. `--include-shared-blocks` deletes them regardless, and the other screenshots that used them will render with gaps.

The exit status is non-zero if anything could not be deleted, and the documents are listed. Blocks are deleted before the layouts that point at them, and the layouts are left alone if a block fails, so running the same command again picks up whatever is left.

### Limits

- Only the screenshot data stream is touched. Step results, network requests, and anything else a monitor recorded stay, as do files written earlier by `extract`.
- Backing indices that are read-only, such as searchable snapshots in the cold or frozen tier, cannot be scrubbed. Those documents are reported as errors.
- Deleted documents stay in the index files on disk until Elasticsearch merges the segments, and in any snapshot taken before the scrub. Run `POST /synthetics-browser.screenshot-*/_forcemerge?only_expunge_deletes=true` to purge them from the indices now, and handle snapshots according to your retention policy.
- Finding the blocks that are still in use reads every screenshot layout in the data stream, which can take a while on clusters with a lot of history.
- Scrub a window that has ended. A monitor run that is storing a screenshot while the scrub is deleting blocks can lose a block it shares with a scrubbed screenshot.

## Output

```
screenshots/
  manifest.jsonl
  <monitor name>/
    <timestamp>_<run id>_step<NN>_<step name>.jpg
```

`manifest.jsonl` gets one line per screenshot written, with the monitor, check group, location, step, step status, and relative path.

Screenshots already on disk are skipped, so rerunning with an overlapping time range only downloads what is new. Use `--overwrite` to rewrite them.

## Development

```
git clone https://github.com/nateshoffner/elastic-synthetics-screenshots
cd elastic-synthetics-screenshots
uv sync
uv run pytest
```

The tests use fake Elasticsearch clients, so no cluster is needed. Bug reports and pull requests are welcome; please include a test with behavior changes.

### How screenshots are stored

Heartbeat does not store a screenshot as one image. It slices each one into an 8x8 grid of JPEG tiles and stores each tile once as a `screenshot/block` document whose `_id` is the tile's content hash. A `step/screenshot_ref` document per step records the full image size and which hash goes at which position. Since most of a page rarely changes between runs, the same tiles are reused across many screenshots.

The extractor pages through the refs with a point-in-time search, fetches the tiles it has not seen yet by id, and pastes them onto a canvas at the recorded positions. Older `step/screenshot` documents that embed the whole image are written out unchanged. The code for this is in `src/elastic_synthetics_screenshots/`: `extractor.py` does the querying and `compose.py` the stitching.

Scrubbing (`scrub.py`) has to know which tiles other screenshots still use. Heartbeat writes the layout at the document root while the index template only maps it under `synthetics.`, so the tile hashes are not indexed. The scrubber reads them from `_source` with a search-time runtime field instead.

## Disclaimer

This is an independent project. It is not affiliated with, endorsed by, or supported by Elastic. Elastic, Elasticsearch, and Kibana are trademarks of Elasticsearch B.V.

The software is provided as is, without warranty of any kind (see [LICENSE](LICENSE)). `scrub` permanently deletes data from your cluster and there is no undo. You are responsible for what you run it against, for checking that you are allowed to delete that data, and for verifying the result.

A successful scrub is not a guarantee that sensitive data is gone everywhere. Copies can remain in snapshots, in index segments that have not been merged, in read-only indices, in other data streams, and in anything that was extracted or exported before the scrub (see [Limits](#limits)). Do not treat this tool as the whole of an incident response or as proof of compliance with any legal or regulatory obligation.

## License

[MIT](LICENSE)
