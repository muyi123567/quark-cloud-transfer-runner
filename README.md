# Quark Cloud Transfer

`quark-cloud-transfer` is a GitHub Actions runtime asset that streams files from
Quark Drive to Google Drive without using the user's PC as the data path and
without staging a complete file on the runner disk.

```text
Quark Web API
    -> GitHub Actions runner memory
    -> Google Drive resumable upload
```

This repository is intentionally a `PLATFORM_RUNTIME_ASSET`. It is not a
canonical Skill and must not be registered in the Kaoyan `SKILL_MANIFEST`.

## Execution repository

The private source repository is:

```text
https://github.com/muyi123567/quark-cloud-transfer
```

GitHub currently blocks private-repository hosted runners for this account due
to its billing state. The operational runner is therefore the public repository:

```text
https://github.com/muyi123567/quark-cloud-transfer-runner
```

The public repository contains only the generic runtime code. `QUARK_COOKIE`
and `GDRIVE_OAUTH_JSON` are still stored as encrypted GitHub Actions secrets.
Use the public runner repository to start transfers.

## What is implemented

- Quark Web cookie injection through GitHub Actions secrets.
- One-time QR helper for creating the Quark cookie without printing it.
- Google Drive OAuth refresh-token injection through GitHub Actions secrets.
- Quark recursive enumeration and exact path or Web FID resolution.
- Quark Range capability probe with a continuous-stream fallback.
- Google Drive resumable upload in bounded in-memory chunks.
- Same-name and same-size duplicate skip.
- Final Google Drive byte-size verification.
- `workflow_dispatch` inputs for query, source path, destination, and dry run.
- Credential redaction in runtime error logs.
- Unit tests and a dependency/import self-test.

## Repository layout

```text
.github/workflows/quark-to-gdrive.yml
src/quark_cloud_transfer/
scripts/quark_login.py
scripts/google_oauth_setup.py
scripts/self_test.py
tests/
```

## First configuration

### 1. Create a private GitHub repository

If the repository does not exist:

```bash
gh repo create quark-cloud-transfer --private --source . --remote origin --push
```

### 2. Create the Quark Web cookie

Install the setup-only dependencies:

```bash
python -m pip install -r requirements-setup.txt
python scripts/quark_login.py --output .quark_cookie.txt
```

Scan the displayed QR code with the Quark app. The helper writes the cookie to
`.quark_cookie.txt` and never prints the cookie value.

Upload it as a repository secret:

```bash
gh secret set QUARK_COOKIE < .quark_cookie.txt
```

Delete the local file after the secret is accepted.

### 3. Create Google Drive OAuth credentials

In Google Cloud Console:

1. Enable the Google Drive API.
2. Configure the OAuth consent screen.
3. Create an OAuth client of type **Desktop app**.
4. Download the client JSON.

Then run:

```bash
python scripts/google_oauth_setup.py \
  --client-secret client_secret.json \
  --output gdrive_oauth.json
gh secret set GDRIVE_OAUTH_JSON < gdrive_oauth.json
```

Delete `gdrive_oauth.json` and `client_secret.json` after the secret is stored.
The OAuth scope is `https://www.googleapis.com/auth/drive`.

For local development checks:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

### 4. Optional Drive root folder

If transfers should stay below a specific Drive folder, create that folder and
set its folder ID:

```bash
gh secret set GDRIVE_ROOT_FOLDER_ID --body "<folder-id>"
```

When unset, the runner uses the Drive account root.

## Run a transfer

Open the operational runner repository:

```text
https://github.com/muyi123567/quark-cloud-transfer-runner/actions/workflows/quark-to-gdrive.yml
```

Then choose `Run workflow`.

GitHub-hosted runners for a private repository require available Actions
minutes and a valid billing/spending-limit state. If the job fails before any
step starts, check `https://github.com/settings/billing/actions`.

Fill exactly one source selector:

- `query`: filename substring, for example `武忠祥`.
- `source_path`: exact Quark path, for example `/27考研/数学/武忠祥.pdf`.
- `source_fid`: exact 32-character Quark Web FID.

Set `destination`, for example:

```text
考研_AI_KB/01_SOURCE_INBOX/数学
```

Run with `dry_run=true` first. The dry run only resolves Quark metadata and
prints the planned files; it does not call Google Drive.

For the real transfer, set `dry_run=false`.

## Runtime behavior

### Quark side

The runner uses the injected Web cookie to list and resolve Web FIDs. For each
selected file it requests a Quark download URL, probes `Range: bytes=0-0`, and
then either:

- fetches bounded Range chunks; or
- reads one continuous Quark response and buffers at most one upload chunk.

### Drive side

The runner creates missing destination folders, checks for an existing file
with the same name and byte size, initiates a resumable upload, sends chunks
with `Content-Range`, and reads the final metadata to verify the byte count.

No complete source file is written to the runner filesystem.

## Security

- Never put cookies, refresh tokens, or OAuth JSON in workflow inputs.
- GitHub Actions masks repository secrets, and this runtime also redacts
  common credential shapes before logging errors.
- Use a private repository and restrict Actions permissions to `contents: read`.
- Rotate `QUARK_COOKIE` if the Quark session is revoked or exposed.
- Rotate the Google OAuth client if `client_secret` or refresh token exposure is
  suspected.

See [SECURITY.md](SECURITY.md) and [REVIEW.md](REVIEW.md).
