# M1-5 private module acceptance

Tracker [#49](https://github.com/coghex/tesserae/issues/49): the combined
private workflow passed on 2026-10-31. A trusted private module, kept outside
this repository, was loaded with `--module-dir`, seeded from copied pilot
state, and played through the owner-selected scene: equipment, one skill check
with a declared spendable resource, and a short single-opponent ranged
encounter. This is a sanitized summary. The private record, with the game,
module, live values, fingerprints and logs, stays in the owner's local private
workspace, and its private commits are reviewed there.

| Child | Delivered by |
|---|---|
| #50: private scene, rule inventory and decisions | Private specification, reviewed privately |
| #51: external package loader | PR #59 |
| #52: `game create`/`serve --module-dir` | PR #60 |
| #53: seeded creation and the pilot converter | PR #61 and a private converter, reviewed privately |
| #54: character and equipment commands | Private module, reviewed privately |
| #55: the skill check and declared resource spending | Private module, reviewed privately |
| #56: the bounded ranged encounter | Private module, reviewed privately |
| #57: this record and the [external module runbook](../external-modules.md) | This PR and a private acceptance record |

## Public checks

The public suite uses only original fixtures, chiefly the Beacon example
package: loading and provider selection, configuration refusals before
listening or creating, seeded creation with ownership checks and no
credentials, and per-participant views. It never needs private files. Ruff and
the GitHub Actions run (Python 3.11 on Ubuntu) pass at each PR's head.

## Private acceptance

| | |
|---|---|
| Tesserae | `e8b8407` (master after PR #61), a clean checkout installed non-editable into a fresh virtual environment; the `tesserae` entry point |
| Private module | A clean checkout at its approved commit; the module's own version and state version pinned in the game's binding |
| Platform | macOS 26.7.1 arm64, Python 3.14.8, websockets 17.4 |
| Network | Loopback only, `serve --authenticated`. No network client was needed; the Mac/Ubuntu VM coverage accepted for M1-3 and M1-4 stands, and physical-device Wi-Fi is not a gate |

- **Inputs.** The converter checked every copied input, and the originals,
  against a fingerprint manifest: the originals were unchanged. Every compared
  value between the copied records and the imported state matched: resources,
  quantities, worn and carried equipment, ammunition, and every derived value,
  recomputed rather than copied.
- **Private tests.** The module's fixed-draw tests passed at the pinned
  revisions. They cover the branches live dice did not reach: failed and
  fumbled checks, critical and fumbled attacks, jams and drops, stopped and
  penetrating hits, limb loss, stun and death saves, recovery, stabilization,
  the aftermath, withdrawal, escape and the round limit.
- **Live run.** Real `game create --seed`, first tokens with `participant
  rotate`, foreground `serve --authenticated --module-dir`, and three `connect`
  processes: two for one player, one for the other. Live dice were kept as
  rolled.
  - Each player's snapshots and results held only their own character. The
    second player's attempts on the first player's equipment, the check and the
    encounter were refused, and nothing changed.
  - Two simultaneous resource-spending checks from one revision gave one result
    and one `stale_revision`; the resource was spent once. A request with an
    old revision was refused as stale.
  - The check succeeded on its live roll, so the opponent started at the far
    end of the lane. In round 1 the opponent missed.
  - At the start of round 2, with combat active (checked before and after),
    the host was stopped and restarted. The last committed request, resent
    unchanged, returned its identical result, and the stored state, history
    and revision were unchanged. Play then continued on the restarted host.
  - In round 2 the player hit twice, the second time against a stunned target
    at +4; the opponent recovered and, wounded, retreated. In round 3 the
    player missed twice at long range, and the opponent escaped.
  - At the end, the displayed sheet equalled the stored one; ammunition and
    the declared resource reconciled exactly; the stored derived values equalled
    a recomputation; the displayed distance and active actor matched the
    stored positions and turn; there was one history entry per revision. No
    token appeared in any output, the second player's output held none of the
    first player's details or rolls, and there was no traceback.
  - Two earlier live runs are not the accepted run. The first stopped at a
    check in the acceptance script itself, which wrongly treated a public
    character ID as private. In the second, the restart came after the fight
    had already ended, not during it. The script was corrected each time, and
    the whole run repeated from a fresh seed and game.
- **Cleanup.** Every run's tokens were revoked and the token files deleted.

## Limits

No AI referee, model conversation, general referee authority or browser client
is claimed; those are M1-6 and later work. No provider was extracted: the
evidence showed no second consumer for these mechanics, so they stay in the
private module.
