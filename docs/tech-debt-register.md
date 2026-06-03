# Tech-Debt Register

A rolling, append-only log of internal messes we have **deliberately chosen not to fix yet**.
The point is to capture the analysis once, so future-us (or a future contributor) does not
have to re-discover and re-reason about the same smell a second time.

## What goes here

- Internal cleanliness / efficiency debt we consciously deferred (duplication, awkward
  abstractions, dead paths, slow-but-working code, naming drift).
- Things that work fine today but will be annoying to live with later.

## What does NOT go here

- **Active bugs / breakage** — fix now, don't log-and-defer.
- **Seam / boundary issues for external tools** — these are not deferrable cleanliness;
  they get handled in the external-tools seam audit, because new surface area will attach there.
- **Open product/architecture questions** — those live in `open-questions.md`.

## How an entry earns its keep

Every entry carries a **Touch trigger**: the natural future moment when we'd be editing that
code anyway. Debt gets paid as a side effect of planned work, not as a separate "cleanup project."
If an entry has no plausible touch trigger and no real cost, it's probably a `wontfix` — say so
and move on. That's the discipline that keeps this list honest instead of turning into a
guilt pile.

## Entry schema

| Field | Meaning |
|---|---|
| **ID** | `TD-NNN`, monotonic, never reused |
| **Logged** | date first noted |
| **Subsystem / path** | where it lives (file, module, or named subsystem) |
| **The smell** | concretely what's messy — enough that we don't re-investigate |
| **Why deferred** | not on current path / works fine / too risky to touch live / etc. |
| **Suspected cost** | what it could bite us on, tagged `hunch` or `measured` |
| **Touch trigger** | the future moment we'd naturally fix it |
| **Size** | S / M / L (rough effort) |
| **Status** | `open` / `scheduled` / `done` / `wontfix` |

---

## Register

<!-- Newest at top. Copy the block below to add an entry. -->

<!--
### TD-NNN — <one-line title>
- **Logged:** YYYY-MM-DD
- **Subsystem / path:** 
- **The smell:** 
- **Why deferred:** 
- **Suspected cost:** (`hunch` | `measured`) 
- **Touch trigger:** 
- **Size:** S | M | L
- **Status:** open
-->

_No entries yet. This file is seeded during the external-tools seam audit; internal smells
we notice along the way land here instead of being fixed mid-audit._
