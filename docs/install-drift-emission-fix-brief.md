# install-drift emission fix — brief

Fix two emission bugs in `scripts/heal_systemd_install_drift.py` (the
`heal-systemd-install-drift` healer), plus a missing runbook. Both bugs were
confirmed via live droplet diagnosis on 2026-06-03: the 12:02 MDT alert batch
shipped alarming/contradictory copy for events that were actually healthy or
mis-classified.

## Repo / baseline
- Repo: ourliberty-agent-core, main @77b532b (or later).
- Primary file: `scripts/heal_systemd_install_drift.py`
- Config: `config/alert-translations.json` (entries under `heal-systemd-install-drift`)
- Tests: `scripts/tests/test_heal_systemd_install_drift.py`

## Bug A — healthy auto-reconcile ships an URGENT "install dance" headline
`_render_content_healed()` (~line 786) is the AUTO-RECONCILED, healthy,
no-action path. It emits subject `install-drift:{unit}` (~line 800).
`config/alert-translations.json` maps the `install-drift` subject to the
imperative "NOT installed / could NOT auto-install / run the install dance"
headline. So a successfully healed event ships a contradictory URGENT alert
carrying a manual-action recommendation. This is the contradiction Larry hit:
headline says "could NOT auto-install" while the detail says "Auto-reconciled."

The sibling `_render_install_healed()` (~line 742) ALREADY solved this exact
problem by emitting subject `install-healed:{unit}`, which routes to a
no-imperative translation entry (`alert-translations.json` line ~71).

Fix: `_render_content_healed` must emit a non-imperative healed subject —
reuse `install-healed:` or add a new `content-healed:` — and
`config/alert-translations.json` must carry a matching no-action entry
(recommended_action = "None — already reconciled; verify with
`systemctl status <unit>`").

DO NOT change `_render_missing_install` (~761) or
`_render_content_drift_dry_run` (~802) for Bug A — those represent
genuinely-broken states and CORRECTLY keep the imperative `install-drift:`
subject.

## Bug B — long-running daemons mislabeled as timer-triggered oneshots
Three spots assume `non-timer == timer-triggered oneshot` and emit
`daemon-reload  # oneshot re-execs on next timer fire` (or "service activated
by its timer"):
- `_render_missing_install` (~768): `enable_line` for the non-timer branch
- `_render_content_healed` (~789): `action_phrase` for the non-timer branch
- `_render_content_drift_dry_run` (~808): `post_line` for the non-timer branch

But `ourliberty-inbox-watcher.service` is `Type=simple` — a long-running
daemon (`WantedBy=multi-user.target`, NO sibling timer). `daemon-reload` alone
NEVER restarts it, so it keeps running stale code (the known
service-restart-after-merge gap). Meanwhile
`ourliberty-heal-tier2-weekly-health-probe.service` IS genuinely
`Type=oneshot`.

Fix: add a classifier that parses `Type=` from the repo unit file and returns
one of three classes:
1. `.timer` — enable/restart the timer (existing timer behavior is correct).
2. oneshot `.service` (`Type=oneshot`) — daemon-reload, re-execs on next timer
   fire (existing copy is correct for this class).
3. long-running `.service` (`Type=simple`/`notify`/`forking`, or no explicit
   `Type=` which defaults to simple) — remediation/message MUST include
   `sudo systemctl restart {unit}` after the daemon-reload, and MUST NOT claim
   "next fire re-execs."

Apply the correct class to all three render functions' remediation text AND to
`_render_content_healed`'s message wording (a `Type=simple` daemon has no
"next fire").

## Tests
Extend `scripts/tests/test_heal_systemd_install_drift.py`:
- Cover all three unit classes (`.timer`, oneshot `.service`, long-running
  `.service`).
- Assert (A) the healthy-heal path emits a subject whose translation entry has
  a no-action / non-imperative `recommended_action`.
- Assert (B) long-running-daemon remediation text contains `systemctl restart`,
  and the oneshot remediation does NOT.
- Keep the existing suite green (it already asserts `'Auto-reconciled'` at test
  line ~1391).

## Runbook (missing — create it)
`runbooks/heal-systemd-install-drift.md` is referenced by every alert's
`recommended_action` but DOES NOT EXIST on the droplet. Create it. It should
explain: the three drift states (missing / content-drift / healed), the 3-way
unit classification, the correct manual remediation per class (timer / oneshot
/ long-running daemon — the last requiring `systemctl restart`), and how to
verify (`systemctl status <unit>`).

## PREFLIGHT MUST VERIFY
- Confirm the exact line numbers above against current main (they may shift).
- Confirm how the subject string flows into the `alert-translations.json`
  lookup (prefix match on the segment before `:`) so the new healed subject
  actually resolves to the new entry — do NOT introduce a subject that falls
  through to a default imperative.
- Confirm no other caller relies on `_render_content_healed` emitting
  `install-drift:`.

## Acceptance
- A healthy auto-reconcile produces a no-action notification (no "install
  dance" imperative, no URGENT severity).
- A `Type=simple` daemon's drift remediation tells the operator to
  `systemctl restart`, not just `daemon-reload`.
- Full test suite green.
