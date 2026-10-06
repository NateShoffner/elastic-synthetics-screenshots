"""Command line interface.

Settings are resolved in this order, highest priority first: command line
flags, environment variables (including those loaded from a .env file), the
TOML config file, built-in defaults.

The scrub command is the exception: what it deletes is decided by its flags
alone, never by the config file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from elastic_transport import TransportError
from elasticsearch import ApiError, Elasticsearch

from .extractor import (
    SCREENSHOT_INDEX,
    STATUS_INDEX,
    Options,
    PartialResultsError,
    extract,
    list_monitors,
)
from .scrub import ScrubOptions, ScrubPlan, plan_scrub, scrub

DEFAULT_ENV_FILE = ".env"
DEFAULT_CONFIG_FILE = "config.toml"

# [connection] keys and their environment variables.
CONNECTION_ENV = {
    "url": "ES_URL",
    "cloud_id": "ES_CLOUD_ID",
    "api_key": "ES_API_KEY",
    "username": "ES_USERNAME",
    "password": "ES_PASSWORD",
    "ca_certs": "ES_CA_CERTS",
}
# Options fields whose [extract] key is spelled differently.
CONFIG_KEYS = {"since": "from", "until": "to", "image_format": "format"}
STATUSES = ("failed", "succeeded", "skipped")
FORMATS = ("jpg", "png")
MAX_ERRORS_SHOWN = 10


def build_parser() -> argparse.ArgumentParser:
    # Flags default to None so resolve() can tell "not given" from a value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", help=f"file with ES_* variables (default: {DEFAULT_ENV_FILE}, if present) [ES_ENV_FILE]")
    common.add_argument("--config", help=f"TOML config file (default: {DEFAULT_CONFIG_FILE}, if present) [ES_CONFIG_FILE]")
    conn = common.add_argument_group("connection (flags override environment variables, which override the config file)")
    conn.add_argument("--url", help="Elasticsearch URL [ES_URL]")
    conn.add_argument("--cloud-id", help="Elastic Cloud ID [ES_CLOUD_ID]")
    conn.add_argument("--api-key", help="encoded API key [ES_API_KEY]")
    conn.add_argument("--username", help="[ES_USERNAME]")
    conn.add_argument("--password", help="[ES_PASSWORD]")
    conn.add_argument("--ca-certs", help="CA bundle path [ES_CA_CERTS]")
    conn.add_argument("--insecure", action="store_true", default=None, help="skip TLS certificate verification")
    common.add_argument("--from", dest="since", help="start of time range, ES date math or ISO 8601 (default: now-24h; required by scrub)")
    common.add_argument("--to", dest="until", help="end of time range (default: now; required by scrub)")
    common.add_argument("--index", help=f"screenshot index pattern (default: {SCREENSHOT_INDEX})")

    filters = argparse.ArgumentParser(add_help=False)
    filters.add_argument("-m", "--monitor", dest="monitors", action="append", help="monitor id, name, or config id; wildcards allowed; repeatable")
    filters.add_argument("-l", "--location", dest="locations", action="append", help="location name (observer.geo.name); repeatable")

    parser = argparse.ArgumentParser(
        prog="elastic-synthetics-screenshots",
        description="Extract or scrub Elastic Synthetics browser monitor screenshots in a cluster.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", parents=[common], help="list monitors that have screenshots in the time range")

    ex = commands.add_parser("extract", parents=[common, filters], help="download screenshots to disk")
    ex.add_argument("-o", "--out", type=Path, help="output directory (default: screenshots)")
    ex.add_argument("--status", choices=STATUSES, help="only steps with this status")
    ex.add_argument("--limit", type=int, help="stop after writing this many screenshots")
    ex.add_argument("--format", dest="image_format", choices=FORMATS, help="format for stitched screenshots (default: jpg)")
    ex.add_argument("--overwrite", action="store_true", default=None, help="rewrite screenshots that already exist on disk")
    ex.add_argument("--page-size", type=int, help="screenshot documents per request (default: 100)")
    ex.add_argument("--status-index", help=f"index pattern holding step results (default: {STATUS_INDEX})")
    ex.add_argument("-q", "--quiet", action="store_true", default=None, help="only print the summary")

    sc = commands.add_parser(
        "scrub",
        parents=[common, filters],
        help="permanently delete screenshots from the cluster",
        description="Permanently delete the screenshots in a time range from the cluster. "
        "Requires --from and --to, and ignores the [extract] table of the config file.",
    )
    sc.add_argument("--dry-run", action="store_true", help="show what would be deleted, then stop")
    sc.add_argument("-y", "--yes", action="store_true", help="delete without asking for confirmation")
    sc.add_argument("--include-shared-blocks", action="store_true", help="also delete image blocks that screenshots outside the selection use")
    sc.add_argument("--report", type=Path, help="write a JSON Lines record of the selected screenshots to this file")
    return parser


def _settings_file(flag: str | None, env_var: str, default: str, kind: str) -> Path | None:
    """The default file is optional; a path given explicitly must exist."""
    explicit = flag or os.environ.get(env_var)
    path = Path(explicit or default)
    if path.is_file():
        return path
    if explicit:
        raise SystemExit(f"error: {kind} file not found: {path}")
    return None


def load_config(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    try:
        with open(path, "rb") as fh:
            config = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"error: invalid config {path}: {exc}")
    for table in ("connection", "extract"):
        if not isinstance(config.get(table, {}), dict):
            raise SystemExit(f"error: [{table}] in {path} must be a table")
    return config


def resolve(args: argparse.Namespace) -> tuple[dict[str, Any], Options, bool]:
    """Merge flags, environment, and config into connection settings, Options, and quiet."""
    env_file = _settings_file(args.env_file, "ES_ENV_FILE", DEFAULT_ENV_FILE, "env")
    if env_file:
        load_dotenv(env_file)  # never overrides variables already set
    config = load_config(_settings_file(args.config, "ES_CONFIG_FILE", DEFAULT_CONFIG_FILE, "config"))
    conn_cfg = config.get("connection", {})
    ext_cfg = config.get("extract", {})

    connection = {
        key: getattr(args, key) or os.environ.get(env) or conn_cfg.get(key)
        for key, env in CONNECTION_ENV.items()
    }
    connection["insecure"] = bool(args.insecure or conn_cfg.get("insecure"))

    values: dict[str, Any] = {}
    for f in fields(Options):
        value = getattr(args, f.name, None)
        if value is None:
            value = ext_cfg.get(CONFIG_KEYS.get(f.name, f.name))
        if value is not None:
            values[f.name] = value
    for name in ("monitors", "locations"):
        if isinstance(values.get(name), str):
            values[name] = [values[name]]
    if "out" in values:
        values["out"] = Path(values["out"])
    for name, choices in (("status", STATUSES), ("image_format", FORMATS)):
        if name in values and values[name] not in choices:
            raise SystemExit(f"error: {CONFIG_KEYS.get(name, name)} must be one of {', '.join(choices)}")

    quiet = bool(getattr(args, "quiet", None) or ext_cfg.get("quiet"))
    return connection, Options(**values), quiet


def make_client(connection: dict[str, Any]) -> Elasticsearch:
    if not connection["url"] and not connection["cloud_id"]:
        raise SystemExit("error: provide --url or --cloud-id (or set ES_URL / ES_CLOUD_ID)")
    kwargs: dict[str, Any] = {"request_timeout": 60}
    if connection["cloud_id"]:
        kwargs["cloud_id"] = connection["cloud_id"]
    else:
        kwargs["hosts"] = connection["url"]
    if connection["api_key"]:
        kwargs["api_key"] = connection["api_key"]
    elif connection["username"]:
        kwargs["basic_auth"] = (connection["username"], connection["password"] or "")
    if connection["ca_certs"]:
        kwargs["ca_certs"] = connection["ca_certs"]
    if connection["insecure"]:
        kwargs["verify_certs"] = False
        kwargs["ssl_show_warn"] = False
    return Elasticsearch(**kwargs)


def describe_plan(plan: ScrubPlan, include_shared: bool) -> str:
    shots = plan.screenshots
    monitors: dict[tuple[str, str], int] = {}
    for shot in shots:
        key = (shot["monitor_id"] or "", shot["monitor_name"] or "")
        monitors[key] = monitors.get(key, 0) + 1
    # Screenshots are listed oldest first.
    lines = [f"selected {len(shots)} screenshot(s), {shots[0]['timestamp']} to {shots[-1]['timestamp']}:"]
    lines += [
        f"{count:>8}  {monitor_id}  {name}"
        for (monitor_id, name), count in sorted(monitors.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    ]
    if include_shared:
        blocks = f"image blocks: {len(plan.blocks)} to delete"
        if plan.shared:
            blocks += f", {len(plan.shared)} of them used by other screenshots, which will be left with gaps"
    else:
        blocks = f"image blocks: {len(plan.blocks - plan.shared)} to delete"
        if plan.shared:
            blocks += f", {len(plan.shared)} to keep because other screenshots use them"
    return "\n".join(lines + [blocks])


def run_scrub(es: Elasticsearch, args: argparse.Namespace, target: str) -> int:
    opts = ScrubOptions(
        since=args.since,
        until=args.until,
        monitors=args.monitors or [],
        locations=args.locations or [],
        include_shared=args.include_shared_blocks,
        index=args.index or SCREENSHOT_INDEX,
    )
    plan = plan_scrub(es, opts)
    if not plan.screenshots:
        print("no screenshots found in the time range", file=sys.stderr)
        return 0
    if args.report:
        with open(args.report, "w", encoding="utf-8") as report:
            report.writelines(json.dumps(shot) + "\n" for shot in plan.screenshots)
    print(describe_plan(plan, opts.include_shared))
    if args.dry_run:
        print("dry run, nothing deleted")
        return 0
    if not args.yes:
        try:
            answer = input(f'Permanently delete these from {target}? Type "yes" to continue: ')
        except EOFError:
            answer = ""
            print()
        if answer.strip().lower() != "yes":
            print("aborted, nothing deleted", file=sys.stderr)
            return 1

    stats = scrub(es, opts, plan)
    summary = f"deleted {stats.screenshots} screenshot(s) and {stats.blocks} image block(s)"
    if stats.kept:
        summary += f", kept {stats.kept} block(s) that other screenshots use"
    print(summary)
    if stats.errors:
        print(f"error: {len(stats.errors)} document(s) could not be deleted:", file=sys.stderr)
        for error in stats.errors[:MAX_ERRORS_SHOWN]:
            print(f"  {error}", file=sys.stderr)
        if len(stats.errors) > MAX_ERRORS_SHOWN:
            print(f"  and {len(stats.errors) - MAX_ERRORS_SHOWN} more", file=sys.stderr)
        print("the scrub is incomplete; fix the cause and run it again", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "scrub" and not (args.since and args.until):
        parser.error("scrub requires --from and --to")
    connection, opts, quiet = resolve(args)
    es = make_client(connection)
    try:
        if args.command == "scrub":
            return run_scrub(es, args, connection["cloud_id"] or connection["url"])

        if args.command == "list":
            monitors = list_monitors(es, opts.index, opts.since, opts.until)
            if not monitors:
                print("no screenshots found in the time range", file=sys.stderr)
            for m in sorted(monitors, key=lambda m: (m["name"] or "", m["id"])):
                print(f"{m['screenshots']:>8}  {m['latest'] or '-':<24}  {m['id']}  {m['name'] or ''}")
            return 0

        log = (lambda _: None) if quiet else (lambda msg: print(msg, file=sys.stderr))
        stats = extract(es, opts, log)
    except (ApiError, TransportError, PartialResultsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        es.close()

    summary = f"wrote {stats.written} screenshot(s) to {opts.out}"
    if stats.existing:
        summary += f", skipped {stats.existing} already on disk"
    if stats.filtered:
        summary += f", {stats.filtered} filtered out by status"
    if stats.incomplete:
        summary += f", {stats.incomplete} with missing blocks"
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
