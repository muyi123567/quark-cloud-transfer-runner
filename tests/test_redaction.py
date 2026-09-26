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
