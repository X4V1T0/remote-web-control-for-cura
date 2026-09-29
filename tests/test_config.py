from RemoteWebControl.config import Config, DEFAULTS, generate_token, parse_config, parse_origins


def test_defaults():
    config, warnings = parse_config({})
    assert warnings == []
    assert config.bind_address == "0.0.0.0"
    assert config.port == 8765
    assert config.token == ""
    assert config.cors_origins == ()
    assert config.max_upload_mb == 200
    assert config.max_upload_bytes == 200 * 1024 * 1024
    assert config.job_retention_days == 7


def test_values_from_cfg_file_are_strings():
    config, warnings = parse_config({"port": "9000", "max_upload_mb": "50", "job_retention_days": "0", "token": " abc "})
    assert warnings == []
    assert (config.port, config.max_upload_mb, config.job_retention_days, config.token) == (9000, 50, 0, "abc")


def test_empty_values_mean_default_without_warning():
    """The documented cura.cfg template leaves every key empty."""
    empty = {name: "" for name in DEFAULTS}
    config, warnings = parse_config(empty)
    assert warnings == []
    assert (config.bind_address, config.port, config.max_upload_mb, config.job_retention_days) == ("0.0.0.0", 8765, 200, 7)
    assert config.token == "" and config.cors_origins == ()


def test_invalid_values_fall_back_to_defaults_with_warning():
    config, warnings = parse_config({"port": "99999", "bind_address": "not-an-ip", "max_upload_mb": "lots"})
    assert config.port == DEFAULTS["port"]
    assert config.bind_address == DEFAULTS["bind_address"]
    assert config.max_upload_mb == DEFAULTS["max_upload_mb"]
    assert len(warnings) == 3
    assert all("remotewebcontrol/" in w for w in warnings)


def test_parse_origins():
    assert parse_origins("") == ()
    assert parse_origins(None) == ()
    assert parse_origins("http://a.local:5173/, https://b.example  http://a.local:5173") == \
        ("http://a.local:5173", "https://b.example")
    assert parse_origins(["*"]) == ("*",)


def test_origin_allowed():
    config = Config("0.0.0.0", 1, "t", ("https://pwa.local",), 1, 1)
    assert config.is_origin_allowed("https://pwa.local")
    assert not config.is_origin_allowed("https://evil.example")
    assert not config.is_origin_allowed("")
    assert Config("0.0.0.0", 1, "t", ("*",), 1, 1).is_origin_allowed("https://any.example")


def test_generate_token_is_random_and_long():
    a, b = generate_token(), generate_token()
    assert a != b
    assert len(a) >= 40
