# Missions delivery roadmap

Status: planned, 2026-11-12. Public tracker [#202](https://github.com/coghex/tesserae/issues/202) and private integration tracker [#203](https://github.com/coghex/tesserae/issues/203).

The [MS-1 contract](missions_contract.md) is proposed in PR #200 under #199.
Runtime work starts after its independent review and merge. Consume the
merged contract and record its exact revision when starting a child.

The owner settled collective abandonment through each active player's
confirmation and all layers of found eggs in the reveal, with ongoing-story
redactions. Tunable generation counts and thread defaults remain policy data
to evaluate using the generated batch.

## Public capability

| Issue | Scope | Prerequisites |
|---|---|---|
| #199 | Review and settle the MS-1 contract | Design #198 and join/leave clarification #201 |
| [#204](https://github.com/coghex/tesserae/issues/204) | add the optional capability and library validator | #199 |
| [#205](https://github.com/coghex/tesserae/issues/205) | implement seeded generation, premades and read-only dry runs | #204 |
| [#206](https://github.com/coghex/tesserae/issues/206) | add transactional records, policy and bounded reads | #204 |
| [#207](https://github.com/coghex/tesserae/issues/207) | implement offers, initial commitment and mission start | #205, #206 |
| [#208](https://github.com/coghex/tesserae/issues/208) | implement active joins, changed terms, departures and abandonment voting | #207 |
| [#209](https://github.com/coghex/tesserae/issues/209) | implement clue observations, visits and objective outcomes | #207 |
| [#210](https://github.com/coghex/tesserae/issues/210) | implement atomic event effects, clock integration and stopping | #209 |
| [#211](https://github.com/coghex/tesserae/issues/211) | implement personal threads, aspirations and selection cooldowns | #207 |
| [#212](https://github.com/coghex/tesserae/issues/212) | implement Easter egg discovery, layers and campaign checklist | #210 |
| [#213](https://github.com/coghex/tesserae/issues/213) | implement closure, carried consequences and participant reveals | #208, #210, #211, #212 |
| [#214](https://github.com/coghex/tesserae/issues/214) | add client protocol messages and guarded host controls | #213 |
| [#215](https://github.com/coghex/tesserae/issues/215) | add referee tools and bounded context integration | #213 |
| [#216](https://github.com/coghex/tesserae/issues/216) | add thin CLI offer, commitment and reveal controls | #214 |
| [#217](https://github.com/coghex/tesserae/issues/217) | add browser offers, participation and reveal controls | #214 |
| [#218](https://github.com/coghex/tesserae/issues/218) | accept the complete model-free CLI and browser lifecycle | #215, #216, #217 |

## Private integration

Public issues carry generic scope and sanitized results. Content, local paths,
source references, private code and evidence stay in the local workspace.

| Issue | Scope | Prerequisites |
|---|---|---|
| [#222](https://github.com/coghex/tesserae/issues/222) | compact legacy scene-correction audit records safely | Independent |
| [#219](https://github.com/coghex/tesserae/issues/219) | implement capability adapters and canonical state adoption | #210, #211, #208 |
| [#220](https://github.com/coghex/tesserae/issues/220) | build the approved library and review a generated batch | #205, #219 |
| [#221](https://github.com/coghex/tesserae/issues/221) | implement the first agreed premade mission | #220 |
| [#223](https://github.com/coghex/tesserae/issues/223) | accept integration and prepare the coordinated live handoff | #202, #222, #219, #220, #221 |

## Suggested working order

Finish #199 first. Then #204 establishes shared types and validation. Generator
#205 and store #206 can proceed separately. The remaining core steps follow
their dependency table. Protocol #214 and bridge #215 can proceed separately
after the core is ready. CLI #216 and browser #217 follow the protocol, then
#218 exercises the public system end to end.

The cleanup #222 is independent. Private adapters/content follow their listed
public prerequisites. #223 checks the complete private integration and prepares
its reviewed live handoff. Executing that handoff follows an owner request.

Mission reports, individual downtime and the separate browser composer/location
redesign remain later work, recorded in the four-priority plan. They are not
hidden completion criteria for these trackers.

