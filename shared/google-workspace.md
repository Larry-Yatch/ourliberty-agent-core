# Google Workspace conventions (read every session if you have Google MCP tools)

This doc applies to **any agent** with Google Drive/Gmail/Calendar MCP tools allowed in its `.claude/settings.json`. As of 2026-05-18 that's Beacon only; Forge/Mirror/Pulse don't have Google access yet. If/when they do, this file is their authority.

## Identity context

- The droplet's Claude Code is authenticated to a **separate Anthropic Max plan**, NOT Larry's personal account.
- That plan is tied to **`agent.beacon.ourliberty@gmail.com`** — the same email is also the agent's Google account.
- All Google MCP calls operate against **this agent Google account's Drive/Gmail/Calendar**, not Larry's personal Google.
- Larry's personal Google is untouchable from this droplet by design.

## The one rule for Drive resources

**Every Drive resource you create (Doc, Sheet, Slide, folder, anything) MUST be inside the `Shared with Larry` folder tree.** Never create at Drive root. Larry has shared `Shared with Larry` to his personal account, and Drive permissions inherit downward — so anything inside this tree is visible to him automatically. Anything outside it is invisible to him without manual sharing.

The way to comply: **always pass `parents` to `create_file`** with the appropriate folder ID below.

## Folder IDs

```
Shared with Larry/  (root)             1tR-tnHGlld-PCPSUAa5MO8QEoVUYnJM7
├── inbox/                              1zFclZ0-1O03PaNmChr3hPUd3yRd4uE5R
├── specs/                              1Fny7kBhuWWZWtY6l9_2Ldbfu0_2YmiJ5
├── notes/                              1hN0Q1amKpstw4QbZlvt8qRhA-sAXpNAB
└── reports/                            17Xp526OfRrnjUkW8OIZfMYurwlF3Xq3I
```

If you ever can't tell which subfolder fits, default to `inbox/`. Larry or you can move it later.

## What goes where

- **`inbox/`** — unsorted, temporary, or "I'm not sure where this belongs" docs. Default landing zone.
- **`specs/`** — specs you've drafted for Forge to build from. The conversational refinement happens elsewhere (Telegram, this doc); only the formal spec document lives here.
- **`notes/`** — meeting notes from conversations with Larry, brain-dump thinking docs, summaries of long discussions.
- **`reports/`** — analytical artifacts: future Pulse cycle summaries, Ledger weekly digests, cost analyses, anything where the output is a report rather than a working doc.

## Naming conventions

Drive sorts alphabetically inside each folder. Pick names that sort sensibly:

| Folder | Pattern | Example |
|---|---|---|
| `notes/` | `YYYY-MM-DD - <topic>` | `2026-05-18 - Phase E5 wiring discussion` |
| `reports/` | `YYYY-MM-DD - <type>` | `2026-05-25 - weekly cost digest` |
| `specs/` | `<feature-slug> - spec` | `dashboard-readonly - spec` |
| `inbox/` | anything; you'll move it out when sorted | (free-form) |

## What you CAN do (allowed tools)

For each Google service, the allowed tools (as of 2026-05-18) are listed in your agent's `.claude/settings.json`. Beacon currently has:

- **Drive:** list_recent_files, search_files, get_file_metadata, read_file_content, create_file, copy_file, download_file_content, get_file_permissions
- **Gmail:** search_threads, get_thread, list_drafts, create_draft, list_labels, create_label, label_message, label_thread, unlabel_message, unlabel_thread
- **Calendar:** list_calendars, list_events, get_event, create_event, update_event, respond_to_event, suggest_time

## What you CANNOT do (intentionally not allowed)

- **Delete anything.** Not events, not labels, not files. If something needs to be deleted, escalate to Larry via Telegram with the file ID + reason.
- **Send email.** `create_draft` is allowed, but actual sending is not. Always leave the draft in Larry's drafts for him to review and send.
- **Modify file permissions.** The Drive connector doesn't expose this anyway, but to be clear: you cannot make a doc public, share it with new users, or change ownership. Permissions inherit from the parent folder; that's the whole sharing model.

## Sharing model — important to understand

- The `Shared with Larry` folder is shared with Larry's personal account at the FOLDER level.
- Anything you create inside it (or in its subfolders) inherits that share automatically. Larry sees it from his personal Drive without any extra step.
- If you accidentally create at Drive root, **Larry will never see it.** You'd need to ask him to manually grant access from the agent account UI, which defeats the purpose.
- Translation: **always pass `parents`**. There is no good reason not to.

## When in doubt

- Sub-folder unclear? → `inbox/`
- Name unclear? → use today's date + a short topic
- Can't pass `parents`? → stop and report; do not create at root as a fallback
- Tool not in your allow list? → escalate to Larry; don't try to find a workaround
