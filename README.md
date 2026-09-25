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

## ChatGPT direct trigger

In addition to the GitHub `Run workflow` button, a trusted writer can update
`requests/current.json` on `main`. That single commit automatically starts
the same workflow. The push-triggered job is owner-gated to `muyi123567`, so
public forks or untrusted actors cannot use this path to run with repository
secrets.

Example:

```json
{
  "query": "武忠祥",
  "source_path": "",
  "source_fid": "",
  "destination": "考研_AI_KB/01_SOURCE_INBOX/数学",
  "dry_run": true,
  "max_files": 10,
  "chunk_mib": 16,
  "duplicate_policy": "skip"
}
```

Exactly one of `query`, `source_path`, or `source_fid` must be non-empty.
Credentials never belong in this file; they remain in GitHub Actions Secrets.

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


## Conditional PDF preprocessing

The same GitHub Actions runner also contains a **conditional** PDF preprocessor:

```text
.github/workflows/pdf-preprocess.yml
scripts/preprocess_pdf.py
```

It is intentionally **not** the default path for every PDF.

Default policy (`mode=auto`):

```text
not a PDF
    -> NO_PREPROCESS_NEEDED

PDF <= 95 MiB
and no explicit upstream read failure
    -> NO_PREPROCESS_NEEDED
    -> send the original PDF directly to Gemini / Spark

PDF > 95 MiB
    -> PREPROCESS_REQUIRED
    -> stream-download from Google Drive
    -> inspect page count / embedded-text coverage
    -> split into bounded PDF segments
    -> upload segments + preprocess_manifest.json
    -> Gemini / Spark continues semantic distillation

PDF <= 95 MiB but upstream explicitly reports
partial / inaccessible / direct-read-failed
    -> PREPROCESS_REQUIRED
```

The threshold is configurable per run with `max_direct_mib`. The default 95 MiB
keeps headroom below the ordinary Gemini per-file document limit.

The preprocessor does **not** perform whole-book OCR by default. Its job is
mechanical: obtain the original bytes, inspect the PDF, and split only when
needed. Gemini remains responsible for visual/formula understanding on the
bounded segments. A bounded segment may later receive targeted OCR if there is
a demonstrated evidence gap.

Manual override modes:

- `auto`: use the policy above.
- `always`: force preprocessing.
- `never`: report only; never split.

`force_reason` is the explicit escape hatch for an upstream failure signal.
A non-empty value forces preprocessing in `auto` mode and is recorded in the
worker output for provenance.

Example for an oversized registered source:

```text
drive_file_id: <Drive file id>
destination: 考研_AI_KB/02_SOURCE_ARCHIVE/books/<source-id>/segments
source_id: <source-id>
job_id: <job-id>
mode: auto
force_reason:
max_direct_mib: 95
target_chunk_mib: 70
max_pages_per_chunk: 60
```

This worker reuses the existing `GDRIVE_OAUTH_JSON` and
`GDRIVE_ROOT_FOLDER_ID` GitHub Actions secrets. No new Google credential is
required.
