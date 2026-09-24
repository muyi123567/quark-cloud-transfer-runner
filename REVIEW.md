# Runtime Asset Review

Date: 2026-09-24

Classification: `PLATFORM_RUNTIME_ASSET / GitHub Actions`

Project canonical Skill status: N/A. This repository is intentionally excluded
from `SKILL_MANIFEST`.

## Source basis

The implementation was derived from:

- The live Kaoyan `SKILL_SOP v1.0.6` and `SKILL_MANIFEST v1.0.1`.
- The Drive asset `README_GEMINI_SETUP.md`.
- The Drive asset `REVIEW_quark-to-gdrive-cloud.md`.
- The Drive archive `quark-gemini-runtime.zip`.
- The authorized local source trees `quarkclouddrive` and
  `quark-largefile-to-gdrive`.

The referenced uploaded Markdown file was not present in the local workspace
at implementation time. The accessible README, review, archive, and source
scripts were used instead.

## Reused atoms

- Quark QR login and `__puus` acquisition.
- Quark Web `file/sort` recursive traversal.
- Quark Web `file/download` download URL acquisition.
- Range probe and continuous-stream fallback.
- Google Drive resumable upload.
- Same-name and same-size duplicate skip.
- Final byte-size verification.

## Runtime changes from the Gemini version

- Removed Gemini CLI and Google Cloud Shell.
- Removed `gcloud auth print-access-token`.
- Added GitHub Actions secret injection.
- Added OAuth refresh-token support.
- Added `workflow_dispatch` inputs for query, source path, FID, destination,
  dry run, maximum files, chunk size, and duplicate policy.
- Added exact path resolution.
- Added credential redaction.
- Added unit tests and a self-test.

## Evaluation design

- Positive path: valid Quark cookie, valid Web FID, valid Drive OAuth refresh
  token, destination path, successful ranged or continuous transfer.
- Auth negative: missing `__puus`, expired cookie, or invalid refresh token must
  fail closed without printing credentials.
- FID negative: an Open-API FID must fail with an explicit Web FID diagnostic.
- Duplicate boundary: same name and same size skips; same name and different
  size fails unless `duplicate_policy=rename`.
- Range boundary: Range support uses bounded per-chunk Quark requests; no Range
  support uses one continuous response and bounded memory.
- Integrity boundary: a final Drive size mismatch fails the run.
- Safety boundary: more matches than `max_files` fail before any transfer.

## Local verification

The repository includes:

- Python unit tests for configuration, redaction, Quark traversal and Range
  probing, Drive duplicate handling, resumable chunk headers, and transfer
  chunking.
- `scripts/self_test.py` for import and dependency checks.
- Syntax compilation through the Python test run.

## Not yet verified externally

- Live Quark Web API behavior against the user's current cookie.
- Quark download URL Range behavior for the target files.
- Live Google Drive OAuth refresh and resumable upload.
- GitHub Actions secret availability and repository workflow execution.
- End-to-end transfer of a real small file.

On 2026-09-24 the first private-repository workflow dispatch was rejected
before step startup:

```text
The job was not started because recent account payments have failed or your
spending limit needs to be increased.
```

This is a GitHub account billing condition, not a runtime-code failure. A
public runner repository was created at
`https://github.com/muyi123567/quark-cloud-transfer-runner`; it contains only
the generic runtime code, while credentials remain encrypted GitHub Actions
secrets.

## End-to-end verification

Successful public-runner execution:

```text
https://github.com/muyi123567/quark-cloud-transfer-runner/actions/runs/35992982264
```

Observed result:

- Source file: `27考研数学武忠祥《高数基础篇》...pdf`
- Source size: `155479921` bytes
- Quark Range support: `true`
- Drive file ID: `1_iS6Y8WUEfjgY0dHsO_3aqghhZmnYGoB`
- Destination parent: `数学` (`1GNhYAEV5bXfEvTEfrxgds-7npJSda2al`)
- Final Drive size: `155479921` bytes

Status: `END_TO_END_VERIFIED`.

## Required first smoke test

1. Store `QUARK_COOKIE`, `GDRIVE_OAUTH_JSON`, and optional
   `GDRIVE_ROOT_FOLDER_ID` as repository secrets.
2. Run the workflow with a small non-sensitive file and `dry_run=true`.
3. Confirm the plan contains exactly the intended file.
4. Run again with `dry_run=false` and a temporary Drive folder.
5. Confirm the `verified` event and identical byte count.
6. Delete the temporary Drive file if desired.

Do not describe the end-to-end path as fully verified until this smoke test
passes.
