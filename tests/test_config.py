import json

import pytest

from quark_cloud_transfer.config import load_settings
from quark_cloud_transfer.errors import ConfigError


def test_loads_oauth_and_quark_secret() -> None:
    settings = load_settings(
        env={
            "QUARK_COOKIE": "a=1; __puus=secret",
            "GDRIVE_OAUTH_JSON": json.dumps(
                {
                    "client_id": "client",
                    "client_secret": "secret",
                    "refresh_token": "refresh",
                }
            ),
        },
        chunk_mib=16,
    )
    assert settings.gdrive_root_folder_id == "root"
    assert settings.chunk_size == 16 * 1024 * 1024
    assert settings.gdrive_oauth is not None
    assert settings.gdrive_oauth["refresh_token"] == "refresh"


def test_dry_run_does_not_require_drive_oauth() -> None:
    settings = load_settings(
        require_drive=False,
        env={"QUARK_COOKIE": "a=1; __puus=secret"},
    )
    assert settings.gdrive_oauth is None


def test_rejects_cookie_without_puus() -> None:
    with pytest.raises(ConfigError, match="__puus"):
        load_settings(
            require_drive=False,
            env={"QUARK_COOKIE": "a=1"},
        )


def test_rejects_non_aligned_chunk_size() -> None:
    with pytest.raises(ConfigError, match="positive"):
        load_settings(
            require_drive=False,
            chunk_mib=0,
            env={"QUARK_COOKIE": "a=1; __puus=secret"},
        )
