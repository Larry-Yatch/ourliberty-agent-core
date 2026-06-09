# Design Pass: Missions Tab Redesign — Work-State Surface v2

**Status:** Design pass — **decisions locked with Larry 2026-06-09** (§7). Ready to spec Phase 0.
**Author:** Claude Code (desktop session, 2026-06-09)
**Approver:** Larry
**Supersedes the model of:** [E4.4f Missions Tab v1](../agents/beacon/specs/e4-4f-missions-tab-v1.md)
**Revives:** `operator-ux-catch-me-up-shortcut`, `operator-ux-gap-log-field` (registered 2026-05-28, never built)
**Anchors to existing substrate:** `scripts/trust_policy.py` (autonomy ladder), `scripts/cleanup_dispatch_branches.py` (GC pattern), `scripts/ceo_digest_generator.py` + Approvals-tab summaries (dashboard digest pattern)

---

## 1. The problem, stated honestly

The Missions tab v1 is **infrastructurally sound and conceptually outgrown.** Its "registry + derive" pattern (Beacon hand-maintains `missions.json`; the dashboard joins it against `chain_events` + PR state to auto-compute phase) is the right pattern. Nothing is rotten. What's wrong is the *scope of reality* it models.

Larry's words (2026-06-09): *"I often get lost in the tasks and the projects and the missions we're working on, especially when I start getting multiple tasks or chats going... I come up with ideas, concepts, or holes that we need to capture and run down later, and I constantly get into a state of fear that we're going to lose it. Then we start setting up timers and stuff that I don't think is the most efficient way."*

This is **two pillars**, not one:

| Pillar | The pain | Today's reality |
|---|---|---|
| **P1 — Live work visibility** | "Where am I across all the work in flight?" | The board sees only **droplet-dispatched** work (forge/mirror/beacon `chain_events`). **Desktop Claude Code chats — the place Larry actually works — emit nothing.** Confirmed: zero desktop-session telemetry feeds `chain_events`. Every concurrent chat is structurally invisible. |
| **P2 — Durable capture of follow-ups** | "Don't lose the idea/hole we said we'd run down later." | No capture surface. Workaround is **ad-hoc timers** (`ScheduleWakeup`, `/loop`, cron) and ephemeral `spawn_task` chips that **don't survive an app restart.** Larry holds the rest in his head and is (rightly) afraid of dropping it. |

Both pillars are the *same underlying gap*: there is no **single, durable work-state surface that spans desktop chats + droplet agents + parked ideas.** Larry *is* the integration layer today. That doesn't scale past ~2 concurrent chats — exactly where he reports losing the thread (he typically runs 3–4).

### Why "outdated" is right but "old code" is the wrong diagnosis
Last meaningful commit to the missions machinery was ~2026-05-30. It isn't stale code. It's a **model that predates the multi-chat desktop workflow.** v1's own problem statement (§1) quotes Larry describing this same scatter on 2026-05-27 — but it only ever fixed the *droplet* slice of it. The desktop explosion happened after.

### The anti-pattern to retire
A **timer is a push at a clock**: you must guess *when*, it fires whether the moment fits or not, and a missed fire is lost. The fix is not a better timer. It is a **durable queue that resurfaces by context** — the item simply *exists* until acted on or dropped, and the system re-raises it when Larry is near the relevant work (or in a periodic dashboard digest). Capture costs one gesture; nothing depends on Larry's memory or a clock he set.

---

## 2. The thesis

> **The Missions tab becomes the single work-state surface for everything in flight or parked — desktop chats, droplet agents, and captured ideas — that Larry never has to hold in his head or babysit with a timer.**

Three commitments:

1. **Desktop sessions are first-class.** A live chat is a card on the board, same as a droplet dispatch.
2. **Capture is one gesture and durable.** Flagging a follow-up never requires authoring a mission, and the captured item outlives the chat, the app restart, everything — until acted on or dropped.
3. **Resurfacing is by context, not clock.** Parked items re-raise when Larry touches related work, or via a dashboard digest — never via a timer.

**Capture broadly, control by cleanup — not by a strict gate.** (Decided 2026-06-09.) Err toward surfacing too much rather than losing something; a **strong GC system** keeps the board from drowning. Larry's framing: *"I'd rather err on having more so we don't lose stuff... we can always tighten up at any time... new options pop up which go into the holding tank."*

Non-goals this arc: unifying Programs (business/Supabase) with Missions (technical), drag-drop, full PM tooling. Bigger swings, not where the pain is.

---

## 3. Data model

### 3.1 A lighter unit: the **thread / capture** — stored in a sibling file *(decided: sibling `captures.json`)*
Today a "mission" is heavyweight — name, brief, `spec_docs`, repo, manually-curated `task_ids`. **That heaviness is why capture doesn't happen.** So captures live in their own version-controlled store and **promote into `missions.json`** only when fleshed out — keeping the curated mission registry clean and meaningful, and keeping "park this" high-volume / zero-ceremony.

```jsonc
// captures.json (sibling to missions.json, version-controlled)
{
  "id": "cap-resurface-stale-pr-comments",
  "title": "Resurface stale PR comments",         // auto-generated from the capture line; editable
  "note": "Mirror sometimes drops review comments on rebase; run this down.",
  "state": "parked",                              // parked → promoted → dropped
  "origin": {
    "source": "desktop-chat" | "telegram" | "agent" | "larry",
    "session_id": "…",                            // the chat that birthed it (desktop)
    "repo": "ourliberty-agent-core",
    "branch": "feat/…",
    "captured_at": "2026-06-09T18:22:00Z"
  },
  "last_touched": "2026-06-09T18:22:00Z",
  "promoted_to": null                             // mission id once promoted
}
```

**Promotion** = create a `missions.json` entry from the capture (name, brief, inferred repo, optional spec stub) and set `promoted_to`. The only moment heavyweight fields appear, and it's deliberate (see §4 promotion authority).

### 3.2 Desktop sessions as `chain_events`
The desktop emits an event class via a Claude Code hook (§5 Phase 0):

```jsonc
{
  "event_type": "desktop_session_start" | "desktop_session_active" | "desktop_session_idle" | "desktop_session_done",
  "agent": "desktop-claude",
  "task_id": "<mission/thread id, or auto-inferred from repo+branch>",
  "payload": {
    "repo": "ourliberty-dashboard",
    "branch": "feat/cleanup-dispatch-branches",
    "title": "Missions tab redesign",
    "blocked_on_larry": false,                    // drives the "needs you" rail (§4)
    "last_activity_ts": "2026-06-09T18:30:00Z"
  }
}
```

The dashboard already joins `chain_events` to missions by `task_id` — a tagged desktop chat appears with **zero new join logic.**

**Entry rule (decided: broad).** A desktop session surfaces if it **touches a tracked repo** — no strict tag requirement. We accept the resulting volume (3–4 live cards is normal) and lean on GC, not a gate, to keep it clean.

### 3.3 The cleanup system — first-class, not an afterthought *(this is the noise control)*
Because capture is broad, **GC is load-bearing.** Modeled on `scripts/cleanup_dispatch_branches.py` (same philosophy, applied to cards). Cards auto-retire on lifecycle signals — no manual tidying:

- **Desktop-session card** → archived when its branch is merged/deleted, **or** the session emits `desktop_session_done`, **or** it's been idle past a staleness window.
- **Capture card** → moves to a collapsed "done" lane when `promoted` or `dropped`; aging-but-parked stays (that's the holding tank, intentionally).
- **Repo closed/archived** → all cards tied to that repo's branches auto-archive in one sweep (Larry: closing/archiving a repo *"should be something that cleans up"*).
- A periodic healer (sibling to the dispatch-branch GC) reconciles drift, so the board self-cleans even if a signal is missed.

---

## 4. UX redesign

The board today answers *"state of dispatched PRs."* It should answer **"where am I, what's waiting on me, and what did we say we'd come back to."**

- **A "Needs you" rail, top and sticky** — everything with `blocked_on_larry` or an operator action (approvals, CLARIFY-exhausted, paused sequences). Subsumes the existing Operator Action Queue panel; first thing the eye lands on; needs-you sorts first, always.
- **Live work lane** — droplet dispatches *and* desktop chats side by side, each with last-activity age and a one-click **resume** (deep-link to the PR, or reopen the chat). 3–4 live cards is the expected norm.
- **Parked lane (captures = the holding tank)** — the durable backlog. Age + origin chat per card. Aging parked items get a gentle nudge, never a nag. GC keeps promoted/dropped items out of sight.
- **Orphans lane shrinks toward zero** with auto-registration (§5 Phase 3).

The 5-column phase kanban stays for *promoted* missions — good at what it does — wrapped by the lanes above so it's no longer the only thing on the page.

### Digest lives on the dashboard, not Telegram *(decided 2026-06-09)*
The "parked & aging — promote / drop / snooze?" + "catch me up" digest renders as a **dashboard surface**, styled like the **Approvals tab's daily/weekly summaries** (built on the `ceo_digest_generator.py` pattern). Telegram DM is secondary/optional. Larry: *"the Telegram chat is not the most effective way to dispatch Beacon... I'd rather consume it on the dashboard and have it visible there."* This also keeps the board as the single surface rather than splitting attention into chat.

---

## 5. Phasing (prove → ship → automate)

| Phase | What | Why this order | Rough size |
|---|---|---|---|
| **0 — Prove the feed** | Desktop-session emitter: a Claude Code `SessionStart`/stop hook posting `desktop_session` `chain_events`. Goal: **this very chat shows up on the board.** | Validates P1 end-to-end for almost nothing. If the feed works, everything else is incremental. | 1 dispatch |
| **1 — Durable capture + GC** | Sibling `captures.json` + one-gesture capture from a desktop chat (I flag → durable card), **plus the §3.3 cleanup healer from day one** (broad capture without GC = a mess). | Kills the P2 fear directly; the cleanup system is what makes broad capture safe. | 2 dispatches |
| **2 — Resurfacing (dashboard digest)** | (a) Context resurfacing: open a repo/mission → related captures surface. (b) Revive `operator-ux-catch-me-up-shortcut` as a **dashboard** digest card (Approvals-summary style), aging = untouched >5 business days. | The **timer replacement** — self-firing by context + a daily dashboard digest, no clock to set. | 2 dispatches |
| **3 — Auto-register + write-back + autonomy ladder** | Auto-claim orphan `task_id`s into proposed threads (retire Orphans lane); defer / resume / reprioritize from the UI (PR-backed); **promotion graduates from manual → auto via `trust_policy.py`** (see §6). | Removes manual-curation drift and starts earning autonomy on low-risk captures. | 2–3 dispatches |
| **4 — Board IA polish** *(optional)* | "Needs you" rail, lane layout, resume affordances. | Pure UX; do once the data is rich enough to arrange. | 1–2 dispatches |

**Build Phase 0 alone first**, look at it together on the live board, then green-light 1→2 as the package that retires the timers-and-fear workflow.

---

## 6. Promotion authority + the earned-autonomy ladder *(decided 2026-06-09)*

**Now:** I can auto-promote a capture into a **fully-staged, ready-to-fire** thread (name, brief, inferred repo, spec stub) — but **dispatching always needs Larry's one gesture.** Removes busywork, keeps him gating spend/outcomes.

**The path to autonomy is already built.** Promotion plugs into **`scripts/trust_policy.py`** — the existing autonomy-tier evaluator Beacon consults (`evaluate(task)` → `auto_approve` / `force_ask` / `reject`, first-match-wins, default empty = everything asks). As proof-of-work accrues, Larry adds rules to `config/trust-policy.json` so **low-risk capture classes auto-dispatch** (e.g. `task_type: doc-only`, single-file, `repos: [agent-core]`) while everything else still asks. This is the same dial Pulse uses to auto-deploy, governed by **Doctrine #48** (`feedback_self_optimizing_config_via_pulse_check_pattern`): a periodic Check can *propose* widening the auto-dispatch rules based on observed success rates, Larry approves the widening. Confidence is earned from data, not granted up front — and the dial stays in his hand.

---

## 7. Decisions locked (2026-06-09)

| # | Question | Decision |
|---|---|---|
| 1 | Capture store | **Sibling `captures.json`**, promote into `missions.json`. |
| 2 | Which desktop chats appear | **Broad** — any chat touching a tracked repo. Control via the **strong GC system (§3.3)**, not a strict entry gate. Tighten later if needed. |
| 3 | Resurfacing | **Daily digest + on-demand**, rendered **on the dashboard** (Approvals-summary style), not Telegram. Context-resurfacing always on underneath. Aging = >5 business days. |
| 4 | Promotion authority | **I prep / Larry dispatches** now; graduate to auto-dispatch for low-risk classes via the **`trust_policy.py` ladder** + Doctrine #48 (§6). |
| 5 | Scope | Programs↔Missions unification, drag-drop, full PM tooling — **out of this arc.** |

---

## 8. Build & orchestration strategy *(decided 2026-06-09)*

Destination: hand the bulk of this to the team via the [build-sequence orchestrator](../agents/beacon/specs/build-sequence-orchestrator.md) — the existing DAG layer (`depends_on` steps, Mirror DAG-preflight, advancer daemon watching `chain_events`, belt-and-suspenders merge gates, pause/resume, ladder UI; Beacon synthesizes the sequence from intent). But two orchestrator constraints shape *how*:

- **Sequences are single-repo in V1** (cross-repo is unbuilt V2). This initiative spans agent-core (emitter, `captures.json`, `trust_policy.py` rules, GC healer) **and** dashboard (lanes, digest, cards). Use the proven **PR-S3 split**: each cross-repo unit becomes **ordered single-repo PRs that both must merge**, agent-core (contract/API) before dashboard (UI), **interface-first** so the UI builds against a frozen contract.
- **One active sequence at a time in V1** + pause-on-failure with `resume`/`cancel`. So we run **one phase per sequence**, not a monolithic 0→4 DAG.

Resulting plan:

1. **Phase 0 — hand-built, NOT a sequence.** It's a novel telemetry source *and* defines the data contracts (`desktop_session` event schema + `captures.json` schema). Prove on a live chat, then **freeze the contracts.** DAGs execute a known plan; they're poor at *discovering* an interface. Phase 0's real deliverable is a frozen contract the downstream sequences build against.
2. **Phases 1–3 — one build sequence each**, interface-first, agent-core steps ordered before dashboard steps (single-repo split). Mirror DAG-preflights; the advancer runs each hands-free; **Larry gates at phase boundaries** (the "look at the board together" checkpoints, which also fit the one-sequence-at-a-time rule).
3. **Phase 4 (UX polish) — hand-driven / iterative.** Board layout benefits from Larry's eyes; least fire-and-forget.

Not blocking, but noted: this redesign is the canonical **two-repo** feature and the natural motivating case to prioritize **orchestrator V2 (cross-repo)** later — keep them decoupled (don't make this the guinea pig for unproven orchestration). The `trust_policy.py` promotion ladder (§6) is a second autonomy proving ground that rides along.

## 9. Recommendation / next step

Decisions are locked. Next deliverable is a **contract-first Phase 0 spec** (desktop-session emitter → `chain_events` → card on the board) — small enough to dispatch as one hand-built PR, designed to **prove on a live chat and freeze the `desktop_session` + `captures.json` schemas** so Phases 1–3 can hand off cleanly to the team as build sequences. Everything reuses existing substrate — `chain_events` join, `trust_policy.py`, the GC healer pattern, the dashboard digest pattern, the build-sequence orchestrator — so this is extension, not reconstruction.
