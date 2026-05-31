# Brief: install-drift auto-remediation (self-heal step C)

Audience: Forge. Target repo: `ourliberty-agent-core`. Single file of real
privileged code + tests. Mirror reviews after.

## Goal

Teach `scripts/heal_systemd_install_drift.py` to **auto-remediate the
missing-install case**, instead of only DM'ing Larry the manual dance.
This closes the operator copy-paste loop for the install-drift alert class.

## Why now

On 2026-05-31 the `ourliberty-heal-resume-paused-on-tier1.{service,timer}`
units (shipped by PR #217) were detected as install-drift; the healer DM'd
Larry the `cp + daemon-reload + enable --now` dance and an operator ran it by
hand. That hand-step is exactly what this removes.

## Key facts (already true on `main`)

- The `larry` user has `NOPASSWD: ALL` sudo and the healer runs as `larry`,
  so **no new sudo grant is needed** — the privileged commands already work
  via `sudo -n`.
- PR #212 (merged) established the **remediate-then-notify** pattern *in this
  same file* for the stuck-timer case:
  - `_heal_stuck_timer(unit)` runs `sudo -n systemctl daemon-reload` then
    `sudo -n systemctl restart <unit>`, returns `(rc, stderr)`, never raises.
  - `_render_stuck_timer_heal(unit, next_fire)` builds the closure DM.
  - `run_once()`'s stuck-timer loop: when enabled, heal inline and DM the
    outcome; when dry-run, DM the recovery command.
  Mirror the missing-install path on this exact shape.
- The missing-install loop in `run_once()` today only calls `dm_larry(...)`
  with `_render_missing_install(unit)` — no remediation.
- `scripts/tests/test_heal_systemd_install_drift.py` exists (27 tests). Extend
  it; do not break it (regression dial 3).

## Build scope

1. **`config/auto-remediation-allowlist.json`** (new):
   ```json
   {
     "$schema_version": 1,
     "_doc": "Allowlist of healer remediation classes permitted to take privileged self-healing action (sudo). A class self-remediates only if listed here AND the healer's enable flag is set AND the kill-switch file is absent. Notify-after: every self-heal emits a notification to Larry. Seeded with install-drift + stuck-timer, both activated immediately per operator decision 2026-05-31.",
     "classes": ["install-drift", "stuck-timer"]
   }
   ```

2. **`_remediation_allowed(class_name: str) -> bool`** in the healer:
   read the allowlist JSON (path relative to repo root, sibling pattern to
   the other config reads), return `class_name in classes`. **Fail safe**:
   on missing file / parse error / unexpected shape, return `False` and
   WARN-log. Never raise.

3. **`_remediate_missing_install(unit: str) -> tuple[int, str]`** (new),
   modeled on `_heal_stuck_timer`:
   - `sudo -n cp <REPO_SYSTEMD_DIR>/<unit> /etc/systemd/system/`
   - `sudo -n systemctl daemon-reload`
   - if `unit.endswith('.timer')`: `sudo -n systemctl enable --now <unit>`
     (a `.service` is activated by its timer — do not enable it directly)
   - use the same subprocess timeout constants / try-except discipline as the
     stuck-timer helpers; return `(rc, stderr)`; never raise.
   - **Verify** after: for a `.timer`, confirm it is active and
     `NextElapseUSecRealtime` is populated (reuse `_systemctl_show`). If
     verification fails, return a non-zero rc with an explanatory stderr so
     the caller falls back to the manual-dance alert.

4. **`_render_install_healed(unit: str, next_fire: str) -> tuple[str, str, str]`**
   (new) — closure DM, mirroring `_render_stuck_timer_heal`:
   - message: plain-language, e.g. `Auto-installed `<unit>` — it was shipped
     in the repo but missing from /etc/systemd/system/. Installed,
     daemon-reloaded, and (for a timer) enabled. Next fire: <next_fire>.`
   - subject: `install-drift:<unit>` (same dedup key as the alert).
   - suggested_action: a one-line verify command,
     `systemctl status <unit>`.

5. **`run_once()` missing-install loop, non-dry-run path** — replace the
   "always DM the manual dance" behavior with:
   - if `_remediation_allowed('install-drift')`:
     - `rc, stderr = _remediate_missing_install(unit)`
     - on `rc == 0`: emit a **notification** via `_render_install_healed` +
       `dm_larry(...)`, `counts['install_healed'] += 1`, `_record_dm(...)`,
       log `auto-installed <unit>`.
     - on `rc != 0`: WARN-log, then fall back to the **current**
       `_render_missing_install` alert + `dm_larry(...)` so Larry still gets
       the manual dance when self-heal fails.
   - else (class not allowlisted): keep current behavior (alert only).
   - The **dry-run** path is unchanged (activation DM).

6. **Counts**: add `install_healed` to the `counts` dict in `run_once()`.

## Tests (extend `test_heal_systemd_install_drift.py`)

- allowlist gate: allowed / not-listed / missing-file / malformed-JSON
  (last two must fail safe = not allowed).
- remediate success → notification path taken, `install_healed` incremented,
  no manual-dance alert emitted.
- remediate failure (non-zero rc) → falls back to manual-dance alert.
- `.timer` vs `.service`: enable line present only for `.timer`.
- not-allowlisted class → alert-only (regression guard for current behavior).
- All `subprocess`/`systemctl`/`cp` calls **mocked** — tests must never touch
  real `/etc/systemd/system` or shell out for real.

## Constraints

- Stdlib only. Match existing logging / heartbeat / state / cooldown helpers.
- No emoji anywhere (code, DMs, comments) — use words.
- Honor the existing kill-switch and `OURLIBERTY_INSTALL_DRIFT_HEALER_ENABLED`
  enable flag in addition to the allowlist (all three gates must pass to act).
- Keep the existing 27 tests green (regression dial 3 — block only on
  PR-introduced failures).

## Out of scope (separate workstreams)

- The scheduled Claude operator (step B) that consumes non-allowlisted alerts.
- Triggering install at merge-time instead of the 12h scan (latency only).
