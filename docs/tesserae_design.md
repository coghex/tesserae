# Tesserae design

Design state: `exploring`

This document records the owner's agreed direction and distinguishes it from
implementation proposals. The public repository is being bootstrapped locally;
there is no remote or tracker yet. No issues have been created.

## Purpose and experience

Tesserae supports tabletop play with an AI referee. The player describes or
selects actions; the referee interprets intent, describes the world, runs NPCs,
and adjudicates uncertain rules. Executable tools resolve supported mechanics
and preserve reliable game state. A browser displays the player's view and offers
interactions. A CLI provides an early development and integration surface.

The diversity of rulesets is a central motivation. A game can use a different
set of concepts and mechanics without being forced into a universal character,
combat, inventory, or turn model.

## Verified starting point

- A new local Git repository with `main` and an initial `.gitignore` commit.
- No pre-existing application, dependency configuration, remote, or tracker.
- A separate private pilot has demonstrated conversational play backed by Python
  dice tools, structured character/campaign records, and a browser map.
- That pilot has not been imported into this repository. Its original records
  and source materials must remain unchanged during bootstrap work.
- The standalone Python dice CLI is now implemented and verified with ten tests;
  live game storage, module loading, browser integration, and model integration
  are not implemented yet.

## Agreed direction

### D-1. Public application with private external content

The application should be publishable independently of the private pilot.
Private rulesets, source materials, adaptations, and campaign files live outside
the repository and load through explicit local configuration. An ignored folder
is not the primary separation mechanism. Tracked history also matters.

Do not copy private game names, paths, examples, source extracts, campaign
records, or game-specific assumptions into public code, tests, fixtures, or docs.
Existing tools must be inspected before any reuse. Publication and license
selection remain separate, explicit steps; this design is not a rights review.

### D-2. AI referee from the beginning

No human referee is required in the intended play experience. The AI referee
handles interpretation and judgment. The player retains control of their
character's decisions and commitments. A ruling is recorded when it changes
ongoing mechanical interpretation, so later sessions can apply it consistently.

### D-3. Optional modules rather than universal game concepts

Dice, maps, inventories, combat, and other features are optional. Descriptive tags
help identify games and features; explicit module selections determine executable
behavior. Adding a descriptive tag alone does not silently activate code.

### D-4. Dependencies refer to required operations

A module declares what it needs from another provider. For example, one crafting
module may require item quantities and transfers from an inventory provider.
Another may require abstract resources instead. The former is not a universal
definition of crafting.

Start with small, explicit contracts. Do not invent a general dependency language
before actual modules establish a need for it.

### D-5. Explicit provider selection and replacement

A configuration chooses which implementation supplies an operation contract.
Two competing providers must produce an understandable configuration error unless
one is explicitly selected. Module load order must not decide which rules win.

Prefer replacement of a whole provider initially. Fine-grained override chains,
hooks, and runtime patching are deferred until a concrete game requires them.

### D-6. Reliable execution and state

Scripts handle arithmetic and other operations that need consistency: resource
changes, item transfers, ammunition, related updates, and action history.
The AI referee supplies decisions and adjudications through explicit operations;
it must not maintain a second authoritative character sheet in prose.

The mechanics and stakes of a consequential roll are established before rolling.
Do not replace or retroactively modify dice because of their results. Explicit
corrections and rulings preserve the original history.

### D-7. Extract boundaries from working play

Begin with one private game module alongside a small original public demonstration
game. Extract components when their responsibility is clear and they can be
demonstrated independently. Do not design a universal rules engine in advance.

### D-8. Bounded first milestone

The target is the already demonstrated style of play: conversational actions,
character/equipment state, maps, checks, combat bookkeeping, recorded rulings,
and persistence. It does not mean completing every rule in the private game.

Keep new features out until that loop works. Crafting, module marketplaces,
multiplayer, elaborate override systems, and additional full games are deferred.

### D-9. One-time local bootstrap delivery

The owner explicitly approved committing the README and these two design documents
locally and fast-forwarding `main` to the completed bootstrap work. This is a
one-time exception because `docs-push` requires an origin and the repository has
none. It does not authorize remote publication or change future delivery policy.

## Proposed implementation boundaries

These are proposals to refine as implementation proceeds, not additional signed-off
product requirements.

### Small host and optional providers

The host loads a selected game, routes commands, coordinates state changes, and
returns a player projection or a referee projection. Feature-specific data and
meaning belong to the selected modules.

An inventory provider can implement ownership, quantities, transfers, and drops.
Using an item delegates its effects to an action or a referee ruling; it does not
universally mean decrementing its quantity. Maps do not assume a particular unit
or that every game uses a grid. Dice arithmetic does not imply a success rule.

### One command path, several clients

CLI, browser, and eventual model tools should invoke the same application commands.
Keep formatting, HTTP, and model-provider code out of mechanical implementations.

Each mutating command should eventually have a stable request ID and expected
state revision. A retry must not repeat a payment, consume another item, or roll
again. Failed validation should leave no partial mutation. Establish the storage
mechanism before implementing these guarantees.

The standalone bootstrap dice utility is not yet a persisted game command and
must be labeled accordingly.

### State and history

Choose one authoritative persisted representation. Keep the resulting actions,
rolls, rulings, and resource changes together with enough provenance to explain
the current state. Portable JSON import/export is desirable; whether live storage
is JSON-based or transactional storage remains open.

Do not call the system event-sourced unless reconstruction from events is actually
implemented and verified. Derived values should be recomputed through the owning
provider rather than edited independently.

### Player and referee views

The browser receives an explicitly constructed player view, not a complete game
state with secrets hidden by CSS. Referee preparation and private source context
remain separate from player payloads. Modules own the disclosure of their fields.

An external module can contain executable Python and should initially be treated
as trusted local code, not a sandboxed third-party package. Remote installation
and an untrusted extension security model are outside the first milestone.

### Import the private pilot deliberately

Do not relocate or rewrite the existing pilot to make the first CLI work. Introduce
an importer later, retain originals, and check resource/gear/scene equivalence.
Review inherited mechanical interpretations rather than treating past play as
proof that every rule implementation is correct.

## Open questions

- **Q-1. Conversation host:** keep conversation in an existing assistant with
  Tesserae alongside it, or embed the conversation and model calls in the app?
  Defer model integration until this is chosen; do not block shared commands.
- **Q-2. Persistence:** file-backed structured records versus transactional local
  storage. Decide before live mutating game commands and recovery guarantees.
- **Q-3. First complete loop:** select the exact original demo scene and the bounded
  private-game mechanics used to validate it. Do not promise full game coverage.
- **Q-4. Module contract:** discover the first provider contracts from the demo and
  private module, then make required operations and compatibility checks explicit.
- **Q-5. Bootstrap delivery:** resolved by D-9. There is still no remote; future
  delivery follows the owner's standing policy unless separately authorized.
- **Q-6. Public release:** select a license and review the public tree and tracked
  history before publication. No publishing is authorized by this design alone.

## Scope discipline

- Every implementation slice produces a runnable behavior or closes a specific
  boundary required by the next slice.
- Do not build features solely because another game might eventually use them.
- Record proposals separately from approved direction; keep unresolved questions
  visible instead of silently making them requirements.
- Tests should verify real invariants and failure behavior, not mirror code.
- Pause for consequential product decisions; resolve ordinary implementation
  details within the agreed scope.

See [Milestone 1](milestone-1.md) for the proposed delivery order and acceptance
signals. This document is not yet a ready-to-file epic or a completed specification.
