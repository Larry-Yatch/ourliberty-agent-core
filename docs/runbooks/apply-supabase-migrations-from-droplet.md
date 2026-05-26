# Apply Supabase migrations from the droplet

Canonical path for applying migrations against `ourliberty-pm-dashboard` (project ref `ezldtkbhexyrgujqmxpd`) from the droplet's Supabase CLI install. Replaces the prior Larry-Mac-only path; the Mac CLI remains valid as a fallback.

Set up 2026-05-26 (Phase E4.4d follow-up). First migration deployed via this path was `0004` at 12:58 UTC.

---

## Prerequisites

The droplet is ready iff all four of the following are true:

1. **Supabase CLI installed at `~/.local/share/supabase/`** (v2.101.0). The 2.x line is shipped as TWO binaries — the user-facing shim `supabase` AND the actual implementation `supabase-go` — that MUST sit in the same directory. A single-binary install (only `supabase`) fails silently on most subcommands. Confirm both:

   ```bash
   ssh larry@134.209.44.80 'ls -la ~/.local/share/supabase/'
   # expect: both `supabase` and `supabase-go` executables
   ```

2. **Project linked at `~/ourliberty-dashboard/.supabase/`**. Confirm:

   ```bash
   ssh larry@134.209.44.80 'cat ~/ourliberty-dashboard/.supabase/config.toml | head -5'
   # expect: project_id = "ezldtkbhexyrgujqmxpd" (or similar)
   ```

   If absent, re-link: see `rotate-supabase-db-password.md` step 6.

3. **Both credentials present in `.env.larry`.** Confirm without printing the values:

   ```bash
   ssh larry@134.209.44.80 'set -a && source /home/larry/credentials/.env.larry && set +a && \
     echo "ACCESS=${SUPABASE_ACCESS_TOKEN:+set} DB_PW=${SUPABASE_DB_PASSWORD:+set}"'
   # expect: ACCESS=set DB_PW=set
   ```

4. **`ourliberty-dashboard` repo checked out at `~/ourliberty-dashboard`** with a writeable origin remote.

---

## One-shot apply

From your laptop's Terminal, with main containing the new migration committed and pushed:

```bash
ssh larry@134.209.44.80 'set -a && source /home/larry/credentials/.env.larry && set +a && \
  cd ~/ourliberty-dashboard && git pull --rebase origin main && \
  PATH="$HOME/.local/share/supabase:$PATH" supabase db push'
```

Expected output: `Applying migration 000N_<name>.sql...` followed by `Finished supabase db push.` — Supabase's CLI prints exactly one apply line per pending migration and a final summary.

**Critical:** use `set -a && source <file> && set +a`, NOT `ssh ... 'KEY=val supabase db push'` (inline `-e` exports) — Supabase CLI reads `SUPABASE_DB_PASSWORD` from the process environment of the `supabase` binary, and the inline form often loses the variable through `ssh`'s arg-splitting when the password contains shell-special chars. This is codified item #57.

---

## Verify (REST round-trip)

After the apply, confirm the new schema is live via a REST round-trip against the service-role key. Substitute `<TABLE>` with a table touched by the migration:

```bash
ssh larry@134.209.44.80 'set -a && source /home/larry/credentials/.env.larry && set +a && \
  curl -sS -o /tmp/verify.json -w "HTTP %{http_code}\n" \
    -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
    -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
    "$SUPABASE_URL/rest/v1/<TABLE>?select=*&limit=1" && cat /tmp/verify.json'
```

Expected: `HTTP 200` + a body of `[]` (table empty) or one row of data. A `404` means the table name is wrong or the migration didn't apply. A `401` means the service-role key is wrong (see `rotate-supabase-keys.md`).

---

## Troubleshooting

### `Invalid access token` from `supabase db push`

The droplet's `SUPABASE_ACCESS_TOKEN` has the wrong format. The Management API requires the `sbp_`-prefixed personal access token, NOT the `eyJ`-prefixed data-plane JWT.

- Check the prefix: `ssh larry@134.209.44.80 'set -a && source /home/larry/credentials/.env.larry && set +a && echo "${SUPABASE_ACCESS_TOKEN:0:4}"'` — expect `sbp_`.
- If wrong, rotate per `rotate-supabase-access-token.md`.

### `password authentication failed` from `supabase db push`

The droplet's `SUPABASE_DB_PASSWORD` doesn't match the project's current DB password. Either the password was rotated and only one side updated, or the install shell-quoting dropped chars.

- Verify the length matches what's in 1Password: `ssh larry@134.209.44.80 'set -a && source /home/larry/credentials/.env.larry && set +a && echo ${#SUPABASE_DB_PASSWORD}'`.
- If divergent, rotate per `rotate-supabase-db-password.md` (which re-links as step 6).

### `Cannot find project ref` / link state lost

The `~/ourliberty-dashboard/.supabase/` directory is missing or corrupt.

- Re-run `supabase link`:

  ```bash
  ssh larry@134.209.44.80 'set -a && source /home/larry/credentials/.env.larry && set +a && \
    cd ~/ourliberty-dashboard && \
    PATH=~/.local/share/supabase:$PATH supabase link --project-ref ezldtkbhexyrgujqmxpd'
  ```

### Migration syntax error mid-apply

Supabase applies migrations in a transaction per file; a failed `0005_foo.sql` rolls itself back, but everything before it stayed. The migration is still listed as pending.

- Fix the syntax error locally in `~/ourliberty-dashboard/supabase/migrations/0005_foo.sql`.
- Commit + push to main.
- Re-run the one-shot apply above. Supabase skips already-applied migrations and re-attempts the failed one.

If the error left the database in an unexpected state (rare — only happens with multi-statement migrations that lacked an explicit `BEGIN/COMMIT`), open the Supabase dashboard SQL editor and manually clean up before re-pushing.

### `supabase: command not found` (or silent no-op)

The CLI install is broken. Most likely the `supabase-go` binary is missing from `~/.local/share/supabase/`.

- Re-install by following the two-binary procedure: download both `supabase` and `supabase-go` from the 2.x release into the same directory, `chmod +x` both. See `docs/operating-manual.md` Part II "Supabase CLI moved to droplet (2026-05-26)" for the install narrative.

### Fallback to Mac CLI

If the droplet is in a bad state and you need to apply a migration NOW:

```bash
cd ~/ourliberty-dashboard && git pull --rebase origin main && \
  source ~/.zshrc && \  # picks up Mac's SUPABASE_ACCESS_TOKEN + DB password
  supabase db push
```

This is the pre-2026-05-26 path; it still works. The droplet path is the canonical one going forward because it removes the Mac-online dependency from the deployment loop.

---

## Related

- Credentials: `rotate-supabase-access-token.md`, `rotate-supabase-db-password.md`, `rotate-supabase-keys.md`
- Install narrative: `docs/operating-manual.md` Part II "Supabase CLI moved to droplet (2026-05-26)"
- Supabase CLI 2.x release notes: https://github.com/supabase/cli/releases
- `supabase db push` docs: https://supabase.com/docs/reference/cli/supabase-db-push
