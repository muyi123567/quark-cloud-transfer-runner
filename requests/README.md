# Transfer request trigger

`requests/current.json` is the machine-triggered entry point for the Quark to
Google Drive workflow.

Changing that file on `main` triggers `.github/workflows/quark-to-gdrive.yml`.
The file contains only non-secret transfer parameters. Credentials remain in
GitHub Actions Secrets.

Schema:

- exactly one of `query`, `source_path`, `source_fid`
- `destination`: required Google Drive destination path
- `dry_run`: boolean, recommended `true` for first resolution
- `max_files`: integer 1..1000
- `chunk_mib`: positive multiple of 0.25
- `duplicate_policy`: `skip` or `rename`
- `file_retries`: optional integer 1..20, Quark CDN attempts per file (default 6)
- `retry_rounds`: optional integer 0..10, sweeps over the failed set (default 2)
- `max_runtime_minutes`: optional number 0..340, soft run budget (default 300)

Only the documented keys are read; any other key is ignored, so a small
`request_id` marker is safe to add when a fresh run is needed without changing
the transfer parameters.

Reruns are idempotent: a file whose name and byte size already exist in the
destination folder is skipped, so re-triggering the same request only transfers
what is still missing.

Do not store cookies, OAuth JSON, tokens, or client secrets in this directory.
