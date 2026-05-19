# Google Workspace conventions (read every session if you have Google MCP tools)

This doc applies to **any agent** with Google Drive/Gmail/Calendar MCP tools allowed in its `.claude/settings.json`. As of 2026-05-19 that's Beacon only; Forge/Mirror/Pulse don't have Google access yet. If/when they do, this file is their authority.

## Identity context

- The droplet's Claude Code is authenticated to a **separate Anthropic Max plan**, NOT Larry's personal account.
- That plan is tied to **`agent.beacon.ourliberty@gmail.com`** — the same email is also the agent's Google account.
- All Google MCP calls operate against **this agent Google account's Drive/Gmail/Calendar**, not Larry's personal Google.
- Larry's personal Google is untouchable from this droplet by design.

## Two MCP servers — who owns what

You have **two** sources of Google tools registered. Use the right one for the job:

| Operation | Use | Tool prefix |
|---|---|---|
| **Docs (create, read, edit, format, comments)** | workspace-mcp | `mcp__workspace-mcp__*_doc*` |
| **Drive (search, list, create, move, share-link)** | workspace-mcp | `mcp__workspace-mcp__*_drive_*` |
| **Gmail (read, draft, label)** | claude.ai Gmail connector | `mcp__claude_ai_Gmail__*` |
| **Calendar (read, create, update events)** | claude.ai Google Calendar connector | `mcp__claude_ai_Google_Calendar__*` |

The claude.ai Google Drive connector is also registered as a **fallback** — its tool names look like `mcp__claude_ai_Google_Drive__*`. **Default to workspace-mcp for any new Drive work.** The claude.ai Drive tools remain only because workspace-mcp is newly wired (as of 2026-05-19) and we want a safety net during week 1. If workspace-mcp turns out reliable, we'll prune the claude.ai Drive tools from the allowlist.

There is **no workspace-mcp Gmail/Calendar coverage** in our current install (`--tools docs drive` only). If you need Gmail/Calendar, use the claude.ai connectors above. Don't try `mcp__workspace-mcp__*_gmail_*` — it isn't there.

## The one rule for Drive resources

**Every Drive resource you create (Doc, Sheet, Slide, folder, anything) MUST end up inside the `Shared with Larry` folder tree.** Never leave anything at Drive root. Larry has shared `Shared with Larry` to his personal account, and Drive permissions inherit downward — so anything inside this tree is visible to him automatically. Anything outside it is invisible to him without manual sharing.

How to comply depends on which tool you use:

- **workspace-mcp `create_doc`** does **NOT** accept a parent folder argument. The doc lands at My Drive root. **You must immediately follow with `update_drive_file(file_id=<doc_id>, add_parents=<folder_id>, remove_parents="root")`** to move it into the correct sub-folder. This two-step pattern is non-negotiable — skip step 2 and Larry can't see the doc.
- **workspace-mcp `create_drive_file` / `create_drive_folder`** accept `folder_id` directly. Pass the appropriate ID from below.
- **workspace-mcp `import_to_google_doc`** — check the schema; if it accepts `folder_id`, pass it; otherwise apply the two-step pattern.
- **claude.ai Drive `create_file`** (fallback) accepts `parents` directly. Pass the appropriate ID.

Verified working pattern for workspace-mcp Docs (smoke-tested 2026-05-19):
```
1. create_doc(title=..., content=...)              → returns doc_id, lands at My Drive root
2. update_drive_file(file_id=doc_id,
                     add_parents=<sub-folder ID>,
                     remove_parents="root")        → moves into Shared with Larry/<sub-folder>
3. (optional) find_and_replace_doc / modify_doc_text / batch_update_doc to populate or edit
4. (optional) get_doc_as_markdown to verify final state
```

Verified: sharing inheritance works on a move (the doc appears in Larry's personal Drive after step 2). You do not need to share or set permissions explicitly.

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

## What you CAN do (allowed tools, as of 2026-05-19)

The authoritative list lives in your agent's `.claude/settings.json`. Summary by source:

**workspace-mcp — Docs (primary editing surface):**
- *Read:* `search_docs`, `get_doc_content`, `get_doc_as_markdown`, `list_docs_in_folder`, `inspect_doc_structure`
- *Create:* `create_doc`, `import_to_google_doc`
- *Edit body:* `modify_doc_text` (insert/replace/format by index), `find_and_replace_doc` (text-based replacement, no index math), `insert_doc_elements` (tables/lists/page breaks), `insert_doc_image`, `update_doc_headers_footers`, `batch_update_doc` (atomic multi-op)
- *Structure/style:* `create_table_with_data`, `update_paragraph_style`, `manage_doc_tab`
- *Comments:* `list_document_comments`, `manage_document_comment`
- *Export:* `export_doc_to_pdf`

**workspace-mcp — Drive (primary for any new Drive work):**
- *Read:* `search_drive_files`, `list_drive_items`, `get_drive_file_content`, `get_drive_file_download_url`, `get_drive_file_permissions`, `get_drive_shareable_link`
- *Create:* `create_drive_file`, `create_drive_folder`
- *Modify:* `update_drive_file` (re-parent, rename, set description), `copy_drive_file`

**claude.ai Gmail:** `search_threads`, `get_thread`, `list_drafts`, `create_draft`, `list_labels`, `create_label`, `label_message`, `label_thread`, `unlabel_message`, `unlabel_thread`

**claude.ai Google Calendar:** `list_calendars`, `list_events`, `get_event`, `create_event`, `update_event`, `respond_to_event`, `suggest_time`

**claude.ai Google Drive (legacy fallback, prefer workspace-mcp):** `list_recent_files`, `search_files`, `get_file_metadata`, `read_file_content`, `create_file`, `copy_file`, `download_file_content`, `get_file_permissions`

## What you CANNOT do (intentionally not allowed)

- **Delete anything.** Not events, not labels, not files. If something needs to be deleted, escalate to Larry via Telegram with the file ID + reason. (workspace-mcp's `--tools docs drive` set doesn't expose delete tools anyway — this is enforced by the server, not just the allowlist.)
- **Modify file/folder sharing permissions.** workspace-mcp HAS `manage_drive_access` and `set_drive_file_permissions`, but they are **intentionally excluded** from your allowlist. You cannot make a doc public, change link sharing, or grant access to new users. The whole sharing model is: things go inside `Shared with Larry/`, inheritance handles the rest. If a user genuinely needs explicit access to something, escalate to Larry.
- **Send email.** `create_draft` is allowed, but actual sending is not. Always leave the draft in Larry's drafts for him to review and send.
- **Trigger OAuth flows.** `start_google_auth` is excluded — auth is an operator-only task, not an agent task.

## Sharing model — important to understand

- The `Shared with Larry` folder is shared with Larry's personal account at the FOLDER level.
- Anything you create inside it (or in its subfolders) inherits that share automatically. Larry sees it from his personal Drive without any extra step.
- If you accidentally create at Drive root, **Larry will never see it.** You'd need to ask him to manually grant access from the agent account UI, which defeats the purpose.
- Translation: **always pass `parents`**. There is no good reason not to.

## Common workflow recipes

### Draft a new spec Doc and land it in `specs/`

```
1. create_doc(title="<feature-slug> - spec", content=<initial template>)
2. update_drive_file(file_id=<new_id>, add_parents="1Fny7kBhuWWZWtY6l9_2Ldbfu0_2YmiJ5", remove_parents="root")
3. get_drive_shareable_link(file_id=<new_id>)   # for posting URL to Larry
```

### Revise a Doc after Larry has edited it

```
1. get_doc_as_markdown(document_id=<id>, include_comments=true)  # see current state including Larry's edits
2. (compute diff vs. your last known version OR identify the section to change)
3. find_and_replace_doc(document_id=<id>, find_text=..., replace_text=...)
   OR modify_doc_text(document_id=<id>, start_index=..., text=..., end_of_segment=true to append)
   OR batch_update_doc(document_id=<id>, operations=[...])  for multiple edits atomically
4. get_doc_as_markdown(document_id=<id>) to verify
```

### Drop a notes Doc summarizing a long conversation

```
1. create_doc(title="YYYY-MM-DD - <topic>", content=<full markdown body, or "" then populate after>)
2. update_drive_file(file_id=<new_id>, add_parents="1hN0Q1amKpstw4QbZlvt8qRhA-sAXpNAB", remove_parents="root")
3. get_drive_shareable_link(file_id=<new_id>) for the Telegram reply
```

## When in doubt

- Sub-folder unclear? → `inbox/`
- Name unclear? → use today's date + a short topic
- Forgot the move-to-folder step after `create_doc`? → run `update_drive_file` immediately; if you don't, Larry can't see the doc.
- Tool not in your allow list? → escalate to Larry; don't try to find a workaround. In particular: any *sharing change* or *delete* always escalates.
- Need Gmail/Calendar but workspace-mcp doesn't have them? → use the claude.ai connectors (`mcp__claude_ai_Gmail__*`, `mcp__claude_ai_Google_Calendar__*`).
