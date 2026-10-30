# Tesserae working notes

Repository: `coghex/tesserae`. Default branch: `main`.

This is a casual hobby project developed manually with the owner. These
project-specific instructions override conflicting global professional-project
workflow instructions and earlier delivery notes in this repository or its issues.

## Manual workflow and owner approval

- Do not use the owner's custom Kanban plugins, skills, coordinator scripts,
  approval labels, readiness gates, or automatic review/merge services here.
- Do not use `docs-push` or a special documentation publication lane. Commit
  documentation with related code using ordinary Git. Documentation-only changes
  can use an ordinary branch/PR too.
- Review PRs manually and report findings and suggestions in the conversation.
  Do not post a formal review, change review labels, or invoke review automation
  unless the owner explicitly requests that action.
- The owner makes the final merge decision. Never merge until the owner approves
  that merge. After approval, run the agreed checks and merge only if they pass;
  report failures rather than silently bypassing them. Do not treat a request for
  an opinion on a PR as merge approval.
- There is no mandatory opposite-brand review, special provenance marker, or
  Kanban issue-approval requirement. Preserve existing attribution as history.
- Prefer small, focused branches/PRs. Keep the main checkout clean. Use an isolated
  worktree when another agent is working; never rewrite or clean another agent's
  branch or worktree.
- Ordinary Git, GitHub CLI, and standard GitHub connector tools are fine.

## Issue work

Agents may draft, file, refine, and link issues without individual approval pauses,
including batches. Stay within the agreed project direction, check for duplicates,
and give issues clear scope and useful acceptance criteria. Record genuinely open
product choices instead of claiming the owner has decided them. Close a child issue
with its PR; keep its tracker open until the full scope is complete.

## Project direction and private content

Read `docs/tesserae_design.md`, `docs/milestone-1.md`, and the relevant delivery
design under `docs/designs/` before changing behavior.

- The public repository contains generic infrastructure and original examples.
  Private source materials, game-specific adaptations, and campaigns stay in an
  explicitly configured external directory.
- The host is authoritative. The first server is foreground, loopback-only,
  SQLite-backed, and uses `websockets`. See tracker #1 and its children.
- Keep the CLI thin and modules optional. Do not assume every game shares
  character fields, dice success rules, inventory, turns, or combat.
- Keep tooling modest. Use meaningful tests for behavior and important failure
  cases; avoid tests that mirror configuration or duplicate library conformance.
- Do not report filed, proposed, or unmerged work as implemented on `main`.

## Local checks

Use a virtual environment. The development setup installs with
`python3 -m pip install -e '.[dev]'` and runs:

```sh
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m unittest discover -s tests -v
```

Local verification may support a manual review. The owner controls final merge
approval; passing checks do not authorize an automatic merge.
