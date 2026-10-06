import pytest

from elastic_synthetics_screenshots.cli import build_parser, resolve


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("ES_URL", "ES_API_KEY", "ES_USERNAME", "ES_ENV_FILE", "ES_CONFIG_FILE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def parse(argv):
    return resolve(build_parser().parse_args(argv))


def test_defaults_without_any_files(workdir):
    conn, opts, quiet = parse(["extract"])
    assert conn["url"] is None
    assert (opts.since, opts.monitors, opts.image_format, str(opts.out)) == ("now-24h", [], "jpg", "screenshots")
    assert quiet is False


def test_env_file_is_loaded(workdir):
    (workdir / ".env").write_text("ES_URL=https://from-env:9200\nES_API_KEY=secret\n")
    conn, _, _ = parse(["list"])
    assert (conn["url"], conn["api_key"]) == ("https://from-env:9200", "secret")


def test_toml_config_sets_defaults(workdir):
    (workdir / "config.toml").write_text(
        '[connection]\nurl = "https://from-toml:9200"\ninsecure = true\n'
        '[extract]\nfrom = "now-7d"\nmonitors = ["a", "b"]\nformat = "png"\nstatus = "failed"\nquiet = true\n'
    )
    conn, opts, quiet = parse(["extract"])
    assert (conn["url"], conn["insecure"]) == ("https://from-toml:9200", True)
    assert (opts.since, opts.monitors, opts.image_format, opts.status) == ("now-7d", ["a", "b"], "png", "failed")
    assert quiet is True


def test_precedence_flags_over_env_over_toml(workdir, monkeypatch):
    (workdir / "config.toml").write_text(
        '[connection]\nurl = "https://from-toml:9200"\napi_key = "toml-key"\n'
        '[extract]\nmonitors = ["from-toml"]\n'
    )
    (workdir / ".env").write_text("ES_URL=https://from-env:9200\nES_API_KEY=env-key\n")
    monkeypatch.setenv("ES_API_KEY", "real-env-key")

    conn, opts, _ = parse(["extract", "--url", "https://from-flag:9200", "-m", "from-flag"])
    assert conn["url"] == "https://from-flag:9200"
    # A variable already in the environment is not overwritten by .env.
    assert conn["api_key"] == "real-env-key"
    # Flags replace the config file's list instead of extending it.
    assert opts.monitors == ["from-flag"]


def test_single_string_monitor_in_toml(workdir):
    (workdir / "config.toml").write_text('[extract]\nmonitors = "only-one"\n')
    _, opts, _ = parse(["extract"])
    assert opts.monitors == ["only-one"]


def test_explicit_config_and_env_paths(workdir):
    (workdir / "prod.toml").write_text('[extract]\nout = "prod-shots"\n')
    (workdir / "prod.env").write_text("ES_URL=https://prod:9200\n")
    conn, opts, _ = parse(["extract", "--config", "prod.toml", "--env-file", "prod.env"])
    assert (str(opts.out), conn["url"]) == ("prod-shots", "https://prod:9200")


def test_missing_explicit_files_fail(workdir):
    with pytest.raises(SystemExit, match="config file not found"):
        parse(["list", "--config", "nope.toml"])
    with pytest.raises(SystemExit, match="env file not found"):
        parse(["list", "--env-file", "nope.env"])


def test_invalid_toml_values_fail(workdir):
    (workdir / "config.toml").write_text("[extract\n")
    with pytest.raises(SystemExit, match="invalid config"):
        parse(["list"])

    (workdir / "config.toml").write_text('[extract]\nstatus = "bogus"\n')
    with pytest.raises(SystemExit, match="status must be one of"):
        parse(["extract"])
