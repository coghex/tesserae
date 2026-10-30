# Milestone 1: one reliable playable loop

Status: implementation beginning; delivery order is a proposal.

## Outcome

A player can view an original small scene and a character, attempt an action,
have the AI referee use executable tools for supported mechanics, see the result
on the map and character view, and reload without losing or duplicating changes.

Validate equivalent workflows with one private game module. No private content
is required to run the public demonstration. Coverage is deliberately bounded:
character/equipment records, checks, movement/display, the selected combat actions,
resource tracking, and recorded rulings. Specify the exact demonstration before
calling the milestone complete.

## Should the CLI come first?

Yes: use a small noninteractive CLI as the first adapter to the application.
It makes commands easy to exercise and lets an external AI referee use the tools
before a model is embedded. Offer human-readable and JSON output.

Do not build a separate terminal RPG, interactive menu system, or text UI. The
browser remains the intended player interface. Both clients must use the same
command handlers. Introduce a thin browser slice early enough to check the actual
experience before attempting broad rules coverage.

## Proposed implementation order

### M1-1. Standalone dice and CLI bootstrap — implemented and verified

- Create a small Python package and `python3 -m tesserae` entry point.
- Support dice arithmetic and inspectable individual results without game rules.
- Supply plain and JSON output; reject malformed expressions safely.
- Keep rolling logic independent of terminal formatting and argument parsing.
- Do not imply that these standalone rolls are persisted campaign actions.
- Verify arithmetic, malformed inputs, absence of implicit critical rules, and
  the actual command-line boundary.

This is a bootstrap utility, not completion of the state/command architecture.

Verification: `python3 -m unittest discover -s tests -v` passes all ten tests,
including command-line subprocess checks. Utility resource limits are 4,096 input
characters, 1,000 total dice, and 1,000,000 sides per die; these are implementation
limits, not rules imposed on all games. Dice support `NdS` (or `dS`), signed
integer terms, and addition/subtraction. No implicit criticals or fumbles.

### M1-2. Decide storage and implement one durable command

Depends on M1-1. Resolve design Q-2 first.

- Establish one authoritative state representation and versioned records.
- Implement a command envelope and a state repository independently of the CLI.
- Prove one mutation survives restart and has an auditable event/result.
- Make a repeated request ID return the same result without applying twice.
- Check invalid requests and stale revisions without partial writes.
- Choose and test the failure boundary for updating state and recording history.

### M1-3. Introduce the original demo and minimal module loading

Depends on M1-2.

- Load an explicitly selected local module and its action definitions.
- Give the public demo a small character and a simple uncertain action.
- Let the module define its fields and check rules; host assumptions stay small.
- Validate only the dependencies/provider selections actually required.
- Report missing providers or conflicting selections before play begins.
- Keep tags descriptive; no implicit execution based on tag names.

Do not add a plugin marketplace, dependency solver, or general override framework.

### M1-4. Connect the first browser slice

Depends on M1-3. Resolve the minimal UI/server choices when needed.

- Show the demo's map, token, character summary, and action result.
- Submit the same commands already exercised by the CLI.
- Provide a deliberate player projection, with tests for undisclosed fields.
- Refresh/reopen and recover the saved state correctly.
- Do not expose the state directory as a general static file server.

Prove one click-to-command-to-saved-result loop before expanding mechanics.

### M1-5. Add the private module and extract the first reusable features

Depends on M1-4. Agree the exact private validation scene first.

- Load the private module/content from a configured directory outside the repo.
- Implement a bounded rules subset with explicit recorded interpretations.
- Inspect existing tools before reuse; do not copy game-specific data publicly.
- Import a copy of pilot state without changing originals; compare key resources,
  quantities, worn/carried gear, and scene position.
- Introduce inventory, equipment, and action/combat providers only as the selected
  loop needs them. Extract shared components where both demos support the boundary.
- Verify provider replacement explicitly instead of relying on load order.

The private module may initially contain most mechanics. Do not manufacture a
large generic abstraction just to make that module small.

### M1-6. Connect the chosen AI-referee workflow and play end to end

Depends on M1-5. Resolve design Q-1 before model integration work.

- Use the shared command surface from the chosen conversation host.
- Preserve player control, declared stakes before rolling, and recorded rulings.
- Keep referee context separate from player payloads.
- Play the original public scene and the agreed private validation scene.
- Confirm displayed and persisted resources agree, retries do not duplicate
  actions/rolls, and closing/reopening recovers the correct point in play.

## Deferred

Additional complete rulesets, crafting, multiplayer, hosting/accounts, marketplaces,
untrusted plugin execution, elaborate override chains, and automatic rulebook-to-code
conversion. No need to settle a model vendor or frontend framework in M1-1.

## Progress and next decision

- Repository initialized on `main`.
- Agreed architecture captured in `tesserae_design.md`; remaining proposals marked.
- M1-1 implemented and verified with ten passing tests.
- Next consequential decision: choose live persistence for M1-2.
