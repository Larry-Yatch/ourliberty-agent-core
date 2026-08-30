# dashboard_review_verdicts

Two VERBATIM Mirror result outboxes captured from the droplet at
`~/agents/outboxes/mirror/.archive/` on 2026-08-27. Both are real
dashboard-dispatched re-reviews that Larry approved from the Approvals tab;
both returned REVIEW_PASS; both were archived by `outbox_notifier`'s
`marker present but no routable target` branch and never auto-merged.

    [notifier] [WARN] marker present but no routable target
      (source=dashboard, original_source=None, agent=mirror); archiving

They are committed unmodified so the reproduction runs against the exact
envelope shape the daemon saw — `source='dashboard'`, no `original_source`,
`reply_chat_id: null` — rather than an invented one.

| file | task | PR | droplet md5 |
|---|---|---|---|
| `mirror-pass-pr1108.json` | `check0-delivered-kinds-tier3-001` | #1108 | `8112ca853d581bc77007907df606defd` |
| `mirror-pass-pr1109.json` | `alert-translations-unrouted-pr-nudges-retired-001` | #1109 | `e05f75908f68d763dc7909c24bd04b6f` |

Do not edit them. To vary a field, copy the dict in the test and override.
