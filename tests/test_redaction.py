from quark_cloud_transfer.redaction import redact_text


def test_redacts_cookie_and_bearer_tokens() -> None:
    text = "Cookie: __puus=abc123; Authorization: Bearer ya29.secret"
    redacted = redact_text(text)
    assert "abc123" not in redacted
    assert "ya29.secret" not in redacted
    assert "<redacted>" in redacted


def test_redacts_refresh_token_json_fragment() -> None:
    text = '{"refresh_token":"1//secret","client_secret":"secret"}'
    redacted = redact_text(text)
    assert "1//secret" not in redacted
    assert '"secret"' not in redacted


def test_redacts_quark_signed_download_url() -> None:
    text = (
        "412 for url: https://dl-pc-zb.drive.quark.cn/path/file"
        "?auth_key=secret-auth&token=secret-token&ork=secret-ork&filename=a.pdf"
    )
    redacted = redact_text(text)
    assert "secret-auth" not in redacted
    assert "secret-token" not in redacted
    assert "secret-ork" not in redacted
    assert "<redacted>" in redacted


def test_redacts_bare_signed_download_path_from_requests_errors() -> None:
    text = (
        "HTTPSConnectionPool(host='dl-pc-zb.drive.quark.cn', port=443): Max retries "
        "exceeded with url: /8JImeSAf/5439150331/530557ed90104ff7b21dfd84723d25ae6"
        "a351a9a/6a351a9ab252731a2b8740828fe0477e6a992577?abt=8_0_&auth_key=secret"
        "&sp=100&token=secret&ork=secret&filename=a.pdf (Caused by ConnectTimeoutError)"
    )
    redacted = redact_text(text)
    assert "8JImeSAf" not in redacted
    assert "auth_key=secret" not in redacted
    assert "ConnectTimeoutError" in redacted


def test_redacts_credentials_inside_a_proxy_url() -> None:
    text = (
        "ProxyError: Cannot connect to proxy "
        "http://alice:s3cret@proxy.example:3128 while reading the CDN"
    )
    redacted = redact_text(text)
    assert "s3cret" not in redacted
    assert "alice" not in redacted
    assert "proxy.example:3128" in redacted
