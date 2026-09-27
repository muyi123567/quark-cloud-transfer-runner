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
- CDN boundary: a Quark CDN connect/read failure, a truncated continuous
  response, or an expired signed URL retries the same file with a freshly
  requested download URL and exponential backoff before the file is failed.
- Batch boundary: a single failed file is recorded and the sweep continues; the
  failed set is revisited in later rounds and reported in the run summary.
- Resume boundary: an interrupted upload continues from the Drive session's
  committed offset instead of restarting the file.
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

## 2026-09-27 CDN resilience hardening

Observed failure: run `36262255348` transferred the 澄潇宇数学大观 package until
`【高数合集】大观知识点.pdf` (24,242,097 bytes, `range_supported: false`) and then
died on the first continuous-stream request:

```text
ConnectTimeoutError(HTTPSConnection(host='dl-pc-zb.drive.quark.cn', port=443),
  'Connection to dl-pc-zb.drive.quark.cn timed out. (connect timeout=120)')
```

Quark metadata, listing, and download-URL generation had all succeeded, so the
defect was not authentication or the Drive side: the migration program had no
file-level tolerance for an unstable mainland CDN reached from an overseas
runner, and one flaky connect aborted the whole 488-file batch.

Changes:

- `QuarkClient` now raises `QuarkCdnError` for connect/read failures, truncated
  streams, and expired signed URLs, and uses a `(20 s connect, 120 s read)`
  timeout pair so a dead node fails over quickly.
- `TransferService` retries each file up to `file_attempts` times with
  5/15/30/60/120 s backoff, requesting a new download URL for the same FID on
  every attempt.
- A failed attempt resumes the Drive resumable session at its committed offset,
  including the continuous-stream path, which skips the committed prefix.
- A file that still fails is recorded and the sweep continues; the failed set is
  retried for `retry_rounds` extra rounds, and a `summary` event reports the
  outcome.
- `--max-runtime-seconds` defers the remaining files once the soft budget is
  spent, so the run always ends with a summary inside the Actions time limit.
- The workflow adds `timeout-minutes: 355`, a pre-transfer unit-test step, a
  `transfer.log` tee, and an always-on job summary listing failures.

Coverage added in `tests/test_retry.py` (plus extensions in `tests/test_quark.py`
and `tests/test_gdrive.py`): retry/backoff with fresh URLs, stream resume from a
committed offset, single-file failure isolation, failure sweeps, same-name and
same-size skip semantics during a retry, runtime-budget deferral, CLI exit codes,
and the Drive session status probe.

## 2026-09-27 Throughput work

Measured baseline from the verified end-to-end run: **2.2 MB/s on one TCP
stream** (7.5 s per 16 MiB chunk, no variance), which is a per-stream ceiling
for an overseas runner pulling from a mainland CDN. With 20.09 GiB and 488
files that is about 2.6 hours, and the size distribution is extremely skewed —
one 5.77 GiB file alone is 28.7% of the package and would occupy a single
stream for ~45 minutes.

Changes:

- `concurrency` (default 4): files are transferred in parallel. Each file keeps
  its own download URL and Drive resumable session, so the only shared state is
  the rotating cookie, the Drive access token, the destination folder cache, and
  the event stream — each now guarded by a lock. The run budget is checked
  before each submission, and a worker never raises into the pool.
- `prefetch` (default 3, for files ≥ `prefetch-min-mib`): several Range chunks
  are fetched from the CDN at once while the Drive upload consumes them strictly
  in order, removing the single-stream floor for the multi-GB files. The window
  is rebuilt from the committed offset on every retry attempt.
- `chunk_mib` default raised to 64 for fewer round trips.
- Connect-class failures (`ConnectTimeout`, `ConnectionError`, `ProxyError`,
  rejected signed URLs) now retry immediately against a fresh URL instead of
  sleeping out the first backoff step.
- `QUARK_PROXY` / `--quark-proxy`: an optional egress proxy applied to the Quark
  leg only. This is the highest-leverage knob when the runner's own route to the
  mainland CDN is the bottleneck. Proxy credentials are redacted from logs.

Coverage added: real parallelism (a barrier proves two files are in flight at
once), one subfolder created once under three workers, failure isolation inside a
parallel sweep, prefetch overlap with strictly sequential Drive chunk order,
prefetch failure resuming at the committed offset, small files staying on the
serial path, immediate retry for connect failures but not read timeouts, proxy
application, `QUARK_PROXY` parsing, and proxy-credential redaction.
