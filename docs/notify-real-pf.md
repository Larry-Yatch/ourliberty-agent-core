# notify-real-pf probe artifact

Exercises the Beacon → Forge dispatch cascade end-to-end:
preflight → CLARIFY_REQUEST → clarification-response → PROCEED →
build-phase re-dispatch → PR.

Clarification resolved to `docs/operating-manual.md` line 258 (the
systemd-units table row) as the contextual anchor for the probe.

No behavior code is touched by this artifact.
