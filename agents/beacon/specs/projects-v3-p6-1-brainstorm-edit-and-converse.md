# projects-v3 P6.1 — Brainstorm card: edit + converse with the team

**Type:** Follow-on to P6 (brainstorm template + auto-fill).
**North Star:** [docs/projects-tab-v3-north-star.md](../../../docs/projects-tab-v3-north-star.md) §7 P6 + §4.3/§4.4/§4.10.
**Depends:** P6 (#611 author + #72 card + #617 derive contract) — all shipped/live.

---

## 0. Desired End State *(the destination)*

**The Brainstorm card is a workspace, not a read-only handoff.** On a Brainstorm-state phase, Larry can:
- **Edit** the AI draft and the "Your decisions" list inline and **save** — his edits persist on the phase and survive reloads, and feed the "Copy handoff for Claude" prompt.
- **Converse with the team** — post a question or a change request in a thread on the card; Beacon answers in her single voice on her next cycle (the same chat that already lives on Parked/Missions cards), so he can refine direction without leaving the board.

Net: the pre-filled brainstorm becomes a place to *work the idea* — edit it, ask the team about it — and only then hand off. The handoff button stays; this removes the "board is read-only, go elsewhere to change anything" friction Larry hit on the first live card.

## 1. Why now

P6 made the brainstorm pre-filled but **read-only**: the only way to change it or ask about it was to copy the handoff into a separate Claude session. Larry, on the first live card, asked for exactly the two missing affordances — edit + a team conversation. The plumbing for both already exists (see §3), so this is assembly, not greenfield, and the context is hot.

## 2. Scope & non-goals

**In:**
- **(agent-core)** A NON-committer write-back endpoint to **edit** a Brainstorm phase's `draft` + `decisions` (persist to `projects.json` on disk; `heal_projects_store` commits — the promote precedent). A **phase card kind** added to the existing card-chat core so a phase gets `GET .../thread` + `POST .../message`, reusing `_post_card_message`/`_card_thread_messages` and the Beacon-answers-next-cycle inbox envelope verbatim.
- **(dashboard)** Inline **edit** of the draft + decisions on the Brainstorm card (save → the new write-back), and the **conversation thread** mounted on the card (mirror the captures thread proxy routes + the `page.tsx` fetch/post + render).

**Out (v1 boundary):**
- **No auto-re-author.** The conversation does NOT automatically rewrite the draft. Beacon answers in-thread; Larry edits the draft himself (or pastes the handoff to Claude as today). Auto-re-authoring the structured draft *from* the thread is a deliberate later nicety — the handoff-to-Claude flow already covers that need.
- No new model call on edit or on the chat post beyond Beacon's existing single-voice cycle. No spec auto-authoring; nothing launches. The Brainstorm→Spec checkpoint is unchanged.
- No multi-user / presence / realtime; saves are last-writer-wins atomic (matches the rest of the store).

## 3. Constraints & reuse *(assembly, not greenfield — verified in code)*

- **Card chat is already kind-generic.** `dashboard_api._post_card_message` / `_card_thread_messages` are store-agnostic over `_CARD_KIND_META` (kinds today: `capture`, `mission`). Adding a `phase` kind = ONE meta entry (`id_key`, `noun`, `thread_url`) + the two phase routes that find the phase in `projects.json` (404) and delegate to the existing cores. The **team reply needs NO new code** — posting drops a generic envelope into Beacon's inbox ("read the thread via GET <thread_url>, answer in your single voice, post a `team_to_larry` card_message for the same <id_key>"); Beacon answers next cycle. Keying: a phase id is unique within a project — thread/message key on `"{project_id}:{phase_id}"` (or the phase id, which is globally unique in practice) so the conversation join key is stable.
- **Edit write-back = the promote precedent.** `_handle_capture_promote`/`_create_project_from_funnel` already rewrite `projects.json` on disk under `_PROJECTS_INGEST_LOCK`, atomic, with `heal_projects_store` as the SOLE git committer. The edit endpoint reads the registry, mutates `phase['brainstorm']['draft']` + `['decisions']`, atomic-writes under the same lock, returns the updated card. Dashboard stays a non-committer.
- **The flat-serve contract already round-trips an edited draft.** `projects_store._phase_card` flattens an 8-section *dict* draft but PASSES THROUGH a plain *string* draft (#617). So a Larry-edited draft is stored as a string on `brainstorm.draft` and serves unchanged — no schema fork. Edited decisions are stored as the fork-string list the author already uses; `_brainstorm_decision_cards` maps them to the card objects.
- **Frontend thread template exists.** Captures thread = Next proxy routes `app/api/missions/captures/[capture_id]/thread|message/route.ts` + `page.tsx` fetch/post + render. Mirror them for `app/api/projects/[project_id]/phases/[phase_id]/thread|message`. Edit UI = an inline editable variant of the existing `BrainstormPrefill` draft/decisions blocks.
- **Plain-language + AI-draft framing preserved** (North Star §4.10): edited text stays in Larry's terms; the draft keeps its AI-draft styling until he edits, after which it's his.

## 4. Risks & guardrails

- **Single-committer invariant.** The edit endpoint MUST go through the `_PROJECTS_INGEST_LOCK` atomic-write path and never git-commit — `heal_projects_store` stays the sole committer (the #592 / promote discipline). A Larry edit and a Narrator re-author must not both commit.
- **Don't clobber an edit with a re-author.** `needs_brainstorm` re-authors only when the phase has no brainstorm or its provenance is from a different state. After a Larry edit, stamp provenance (`by: 'larry'`, `edited_at`) so the Narrator sweep treats it as authored-and-edited and does NOT overwrite it. (Idempotency guard already keyed on provenance.)
- **Durable chat.** Reuse the existing `card_message` contract: a post 503s if Supabase is down (never silently dropped); the thread degrades to empty when Supabase is unavailable (read-resilience parity with the derive).
- **Auth parity.** Edit + message routes take `X-Dashboard-Token` + an allowlisted `X-Actor` (the existing write-route posture); the thread GET takes the dashboard token.
- **Graceful empties.** An empty edited draft clears the draft block (no-prefill), exactly as today; an empty decisions list renders nothing.

## 5. Done-gate *(BROWSER-CHECKABLE — run live on the board)*

- [ ] **Edit draft:** edit the AI draft on a Brainstorm card, save, reload — the edit persists and the card shows the edited text.
- [ ] **Edit decisions:** add/change/remove a "Your decisions" item, save, reload — persists.
- [ ] **Handoff reflects edits:** "Copy handoff for Claude" includes the edited draft + decisions.
- [ ] **Converse:** post a question on the card; it appears in the thread; Beacon posts a `team_to_larry` reply on her next cycle (verify the envelope lands in her inbox and a reply appears).
- [ ] **No clobber:** after a Larry edit, the next projects-store GC tick does NOT overwrite his edited draft.
- [ ] **Non-committer held:** the edit writes `projects.json` on disk; `heal_projects_store` commits the delta; no second committer, no git write from dashboard-api.

## 6. Breakdown (steps → DAG)

1. **p6_1-phase-brainstorm-edit-backend** *(ourliberty-agent-core)* — **End state:** *a phase's brainstorm draft + decisions can be edited and persisted, non-committer.* Add `POST/PATCH /api/projects/{project_id}/phases/{phase_id}/brainstorm` editing `draft` (string) + `decisions` (fork strings), atomic-write under `_PROJECTS_INGEST_LOCK` (promote precedent), stamp `brainstorm_provenance.by='larry'`/`edited_at` so the Narrator won't clobber it. Tests for persist + idempotent re-author guard. *(no deps)*
2. **p6_1-phase-card-thread-backend** *(ourliberty-agent-core)* — **End state:** *a Brainstorm phase has a team-chat thread.* Add a `phase` kind to `_CARD_KIND_META`; add `GET .../thread` + `POST .../message` routes that resolve the phase in `projects.json` (404) and delegate to `_card_thread_messages` / `_post_card_message`; confirm Beacon's cycle picks up the generic envelope. Tests mirror the captures thread tests. *(no deps)*
3. **p6_1-brainstorm-card-edit-ui** *(ourliberty-dashboard)* — **End state:** *edit + save on the card.* Inline-editable draft + decisions on `BrainstormPrefill`, save → step 1's endpoint (+ Next proxy route), optimistic update, graceful empties. Tests for edit/save/handoff-reflects-edits. *(dep: 1)*
4. **p6_1-brainstorm-card-chat-ui** *(ourliberty-dashboard)* — **End state:** *converse on the card.* Mount the conversation thread on the Brainstorm card (mirror the captures thread Next proxy routes + `page.tsx` fetch/post + render). Tests for post/render/empty-degrade. *(dep: 2)*

**DAG:** {1 → 3} and {2 → 4} (two independent backend→frontend chains; ship edit and chat separately). **Closeout MUST confirm every §5 item live in the browser, including a real Beacon reply landing in the thread.**
