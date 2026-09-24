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
- `max_files`: integer 1..100
- `chunk_mib`: positive multiple of 0.25
- `duplicate_policy`: `skip` or `rename`

Do not store cookies, OAuth JSON, tokens, or client secrets in this directory.
