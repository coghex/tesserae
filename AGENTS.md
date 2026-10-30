# Working in Tesserae

Repository: `coghex/tesserae`. Default branch: `main`. Keep the primary checkout
clean and update it only by fast-forwarding the default branch.

Read `docs/tesserae_design.md`, `docs/milestone-1.md`, and the relevant delivery
design under `docs/designs/` before changing behavior.

## Project-specific authority for issue work

The owner explicitly authorized agents to draft and file all four initial server
children and to file any issues they see fit for this hobby project. This overrides
global/skill requirements to pause for owner signoff before issue creation or to
file only one approved issue per turn. It applies to Tesserae only.

- Draft, file, refine, and link issues autonomously within the project's agreed
  direction. Routine decomposition and issue-specification details do not need a
  separate approval checkpoint. Batch filing is allowed.
- Keep verification, deduplication, clear acceptance criteria, dependencies, and
  one-PR-sized scopes. Preserve explicit unresolved product choices in affected
  issues rather than presenting them as owner-approved decisions.
- Do not stop unrelated issue filing while an implementation choice is pending.
- This permission does not waive independent code review, CI, privacy boundaries,
  or the owner's other working agreements.

## Code and documentation delivery

- Implement code in an isolated worktree under `~/worktrees/coghex/tesserae/`,
  with one child issue, one branch, and one PR. PRs close their child issue and
  reference a tracker without closing it prematurely.
- Follow the owner's current-head independent-review and CI gates. Preserve PR
  origin markers. Issue-filing autonomy is not permission to self-approve code or
  push implementation directly to `main`.
- Publish documentation with `docs-push`; the earlier no-origin exception ended
  when the GitHub remote was configured. Link associated documentation commits
  from code PRs. Markdown classified as code by that tool follows the code lane.

## Scope and private content

- The public repository contains generic infrastructure and original examples.
  Private source materials, game-specific adaptations, and campaigns stay in an
  explicitly configured external directory.
- The host is authoritative. The first server scope is foreground, loopback-only,
  SQLite-backed, and uses `websockets`. See tracker #1 and its children.
- Keep the CLI thin and modules optional. Do not assume all games share character
  fields, dice success rules, inventory, turns, or combat.
- Add focused tests for actual command, persistence, and connection invariants.
  Do not duplicate transport-library conformance or exhaustive terminal tests.

## Verification

Current test entry point: `python3 -m unittest discover -s tests -v`.
Installation, protocol, storage, and server acceptance requirements are specified
by the relevant child issue. Do not report filed or approved work as implemented.
