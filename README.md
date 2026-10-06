# elastic-synthetic-screenshot-extractor

[![CI](https://github.com/nateshoffner/elastic-synthetic-screenshot-extractor/actions/workflows/ci.yml/badge.svg)](https://github.com/nateshoffner/elastic-synthetic-screenshot-extractor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Pulls screenshots captured by Elastic Synthetics browser monitors out of an
Elasticsearch cluster and writes them to disk as image files.

Synthetics stores each screenshot as a layout document plus deduplicated image
blocks in the `synthetics-browser.screenshot-*` data stream. This tool reads
the layouts, fetches the blocks, and stitches the full images back together.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```
uv sync
```

## Configuration

Settings come from two files, both git ignored, each with a committed example:

- `config.toml` holds the cluster URL, time range, monitors, and output
  options. Copy `config.example.toml` to `config.toml`; it documents every
  key. Credentials may go here too.
- `.env` is the place for credentials if you prefer to keep them separate.
  Copy `.env.example` to `.env` and set `ES_API_KEY`, or `ES_USERNAME` and
  `ES_PASSWORD`.

Both are loaded from the current directory automatically. Point at other files
with `--config` / `ES_CONFIG_FILE` and `--env-file` / `ES_ENV_FILE`.

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

Precedence, highest first: command line flags, environment variables
(including `.env`), the config file, built-in defaults. See
[Permissions](#permissions) for what the credentials need to be allowed to do.

The credentials need `read` on `synthetics-browser.screenshot-*` and
`synthetics-browser-*`.

## Permissions

The tool only runs searches and point-in-time reads, so the credentials need
the `read` index privilege on two data stream patterns and no cluster
privileges:

| Pattern | Used for |
| --- | --- |
| `synthetics-browser.screenshot-*` | screenshot layouts and image blocks |
| `synthetics-browser-*` | step results, to record and filter by step status |

If you pass a custom `--index` or `--status-index`, grant `read` on those
instead.

The simplest option is an API key scoped to exactly that, created by a user
who can manage API keys:

```
POST /_security/api_key
{
  "name": "synthetics-screenshots",
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

If you would rather use a role and a user or long-lived key, the equivalent
role is:

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

For a cross-cluster search setup, grant the same `read` privilege on the
remote cluster and pass the remote pattern, for example
`--index "remote:synthetics-browser.screenshot-*"`.

## Usage

See which monitors have screenshots:

```
uv run synthetics-screenshots list --from now-7d
```

Extract screenshots:

```
# everything from the last 24 hours
uv run synthetics-screenshots extract

# one monitor, last 7 days, failed steps only
uv run synthetics-screenshots extract -m "Checkout flow" --from now-7d --status failed

# wildcard match, specific location, lossless output
uv run synthetics-screenshots extract -m "checkout*" -l "US East" --format png -o ./out
```

`--monitor` matches the monitor id, name, or config id. `--from` and `--to`
accept Elasticsearch date math (`now-7d`) or ISO 8601 timestamps. Run
`uv run synthetics-screenshots extract --help` for all options.

## Output

```
screenshots/
  manifest.jsonl
  <monitor name>/
    <timestamp>_<run id>_step<NN>_<step name>.jpg
```

`manifest.jsonl` gets one line per screenshot written, with the monitor,
check group, location, step, step status, and relative path.

Screenshots already on disk are skipped, so rerunning with an overlapping time
range only downloads what is new. Use `--overwrite` to rewrite them.

## Development

```
git clone https://github.com/nateshoffner/elastic-synthetic-screenshot-extractor
cd elastic-synthetic-screenshot-extractor
uv sync
uv run pytest
```

The tests use a fake Elasticsearch client, so no cluster is needed. Bug
reports and pull requests are welcome; please include a test with behavior
changes.

### How screenshots are stored

Heartbeat does not store a screenshot as one image. It slices each one into
an 8x8 grid of JPEG tiles and stores each tile once as a `screenshot/block`
document whose `_id` is the tile's content hash. A `step/screenshot_ref`
document per step records the full image size and which hash goes at which
position. Since most of a page rarely changes between runs, the same tiles
are reused across many screenshots.

The extractor pages through the refs with a point-in-time search, fetches the
tiles it has not seen yet by id, and pastes them onto a canvas at the recorded
positions. Older `step/screenshot` documents that embed the whole image are
written out unchanged. The code for this is in `src/synthetics_screenshots/`:
`extractor.py` does the querying and `compose.py` the stitching.

## License

[MIT](LICENSE)
