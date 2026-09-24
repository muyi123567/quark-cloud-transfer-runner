# Security Notes

## Credentials

The runtime expects:

- `QUARK_COOKIE`: Quark Web cookie containing `__puus`.
- `GDRIVE_OAUTH_JSON`: Google OAuth refresh-token JSON with `client_id`,
  `client_secret`, and `refresh_token`.
- `GDRIVE_ROOT_FOLDER_ID`: optional Drive folder ID.

Store these only as GitHub Actions secrets. Do not place them in workflow
inputs, source files, issues, logs, or artifacts.

## Logging

The CLI emits JSON-line events. Error messages pass through a redaction filter
for Bearer tokens, OAuth token fields, cookies, and `__puus`. The runtime never
prints the Quark download URL or Google access token.

## Data path

The runner uses bounded in-memory chunks. It does not create a complete local
copy of the source file. GitHub-hosted runners are ephemeral and are discarded
after the job.

## Least privilege

The workflow uses `permissions: contents: read`. The Google OAuth scope required
for duplicate checks, folder creation, upload, and verification is
`https://www.googleapis.com/auth/drive`.

## Rotation

Rotate `QUARK_COOKIE` after a suspected session compromise. Revoke the Google
OAuth refresh token or rotate the OAuth client if the credential JSON is
exposed.
