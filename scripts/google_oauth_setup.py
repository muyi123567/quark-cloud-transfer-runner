#!/usr/bin/env python3
"""One-time Google Drive OAuth helper.

Create a Google OAuth Desktop app client, download its JSON, then run this
helper locally. The resulting JSON is stored as the GDRIVE_OAUTH_JSON secret.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Drive OAuth refresh token.")
    parser.add_argument(
        "--client-secret",
        required=True,
        help="Downloaded OAuth Desktop app client JSON.",
    )
    parser.add_argument(
        "--output",
        default="gdrive_oauth.json",
        help="Output path for the GDRIVE_OAUTH_JSON secret.",
    )
    args = parser.parse_args()

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, SCOPES)
    credentials = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )
    if not credentials.refresh_token:
        raise RuntimeError("Google did not return a refresh_token; revoke and retry consent.")

    output = Path(args.output).expanduser().resolve()
    output.write_text(
        json.dumps(
            {
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "refresh_token": credentials.refresh_token,
                "token_uri": credentials.token_uri,
                "scopes": list(credentials.scopes or SCOPES),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("GDRIVE_OAUTH_OK")
    print(f"Credential written to: {output}")
    print("Next: upload it with `gh secret set GDRIVE_OAUTH_JSON < gdrive_oauth.json`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
