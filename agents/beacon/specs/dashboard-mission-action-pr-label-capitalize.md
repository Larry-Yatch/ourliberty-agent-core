# Capitalize the mission-action "PR open" label

**Step id:** `mission-action-pr-label-capitalize`
**Target repo:** `ourliberty-dashboard`
**Type:** UI papercut (one-line fix)

## Desired end state

On the Missions board, when a mission action (Defer / Resume / Reprioritize) has
an open PR, the status line reads **"Defer PR open — review & merge ↗"** with the
action name properly capitalized — not the current lowercase **"defer PR open —"**.

## Problem

`app/missions/components/MissionActionBar.tsx` renders the pending-PR status by
interpolating `pendingPr.action` raw:

```tsx
{pendingPr.action} PR open —{" "}
```

`pendingPr.action` is a `MissionAction` enum whose values are lowercase
(`"defer"`, `"resume"`, `"reprioritize"`, …), so the line renders at the start of
a sentence with a lowercase word — a small but real grammar papercut.

## The fix

Capitalize the first letter of the action for display only (do not change the
underlying value, the enum, or any request payload). A minimal, general fix that
works for every `MissionAction` value:

```tsx
{pendingPr.action.charAt(0).toUpperCase() + pendingPr.action.slice(1)} PR open —{" "}
```

(Equivalently, a tiny local `capitalize(s)` helper or a CSS `capitalize` class —
implementer's choice, but the rendered first letter must be uppercase.)

## Acceptance criteria

- The pending-PR status line shows the action capitalized (e.g. "Defer PR open —").
- No change to `MissionAction` values, request payloads, or the action buttons.
- If a component/snapshot test asserts the old lowercase text, update it to the
  capitalized form; otherwise no new test is required for a display-only change.
- `npm run lint` / `npm run build` (or the repo's CI checks) pass.

## Out of scope

- Any other label, component, or behavior on the Missions board.
- The action enum, the API, or the underlying mission-action flow.
