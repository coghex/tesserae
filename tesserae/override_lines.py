"""How the streaming client shows negotiated creation overrides to a person
(docs/designs/creation_overrides_contract.md section 12): lines for each
override message, and the shapes it checks before showing one. The host
owns every rule; these only say what it sent, and never call an offer
applied before an acceptance has applied it."""

import json

# Every state a request for a host decision may be in: legacy and negotiated.
LEGACY_STATES = ("pending", "approved", "denied", "cancelled", "superseded", "stale")
OVERRIDE_STATES = (
    "pending_host", "awaiting_player", "applied", "denied", "declined", "withdrawn",
    "cancelled", "superseded", "stale",
)  # fmt: skip
OPEN_STATES = ("pending_host", "awaiting_player")


def compact(value) -> str:
    return json.dumps(value, separators=(", ", ": "), ensure_ascii=False)


def short(digest) -> str:
    return digest[:12] if isinstance(digest, str) else "?"


def awaited(request: dict, role: str | None) -> str:
    """Whose response a request waits for, in words."""
    state = request["state"]
    offer = request.get("offer")
    if state == "pending_host":
        return "waiting for the host"
    if state == "awaiting_player" and offer is not None:
        if not offer.get("current"):
            why = offer.get("stale_reason") or "stale"
            return f"offer {offer['offer_revision']} no longer holds ({why})"
        who = "the player" if role == "host" else "you"
        return f"offer {offer['offer_revision']} waiting for {who}, not applied yet"
    if state == "applied":
        return "applied"
    return state


def request_line(request: dict, role: str | None) -> str:
    who = request["requester"]["name"] or request["requester"]["participant_id"]
    categories = ", ".join(request["categories"])
    if request.get("categories_more"):
        categories += f" and {request['categories_more']} more"
    return (
        f"[override] {request['proposal_id']} {awaited(request, role)}: {who} asks for "
        f"{request['target']['id']} ({categories}): {compact(request['explanation'])}"
    )


def preview_lines(preview: dict, digest, tag="[preview]") -> list[str]:
    """A preview, every part of it: what would happen, exactly."""
    lines = [f"{tag} {preview['headline']} (digest {short(digest)})"]
    for effect in preview["effects"]:
        lines.append(f"{tag}   effect: {effect['summary']}")
    for name in ("waivers", "allowances"):
        for entry in preview[name]:
            lines.append(f"{tag}   {name[:-1]}: {entry['summary']}")
    for step in preview["later_steps"]:
        lines.append(
            f"{tag}   later: {step['command']}: {step['summary']} ({step['stakes']})"
        )
    for text in preview["consequences"]:
        lines.append(f"{tag}   consequence: {text}")
    for text in preview["limitations"]:
        lines.append(f"{tag}   cannot: {text}")
    return lines


def offer_line(offer: dict) -> str:
    state = offer["state"]
    if state == "open":
        state = "open, not applied yet" if offer.get("current") else (
            f"no longer holds ({offer.get('stale_reason') or 'stale'})")  # fmt: skip
    elif offer.get("stale_reason"):
        state += f" ({offer['stale_reason']})"
    note = f"; note: {compact(offer['note'])}" if offer.get("note") else ""
    return (
        f"[offer] {offer['offer_id']} revision {offer['offer_revision']} "
        f"({offer['origin']}), {state}: {offer['headline']}{note}"
    )


def lines(message: dict, role: str | None = None) -> list[str]:
    """An override message as lines for a person."""
    kind = message["type"]
    if kind == "override_categories":
        return [f"[overrides] {c['category_id']} ({c['label']}): {c['policy']}"
                for c in message["categories"]] or ["[overrides] No categories."]  # fmt: skip
    if kind == "override_discover":
        found = []
        for item in message["items"]:
            if "quality_id" in item:
                actions = "; ".join(
                    f"{a['action_id']} {compact(a['parameters'])}"
                    for a in item["actions"]
                )
                found.append(
                    f"[overrides] {item['quality_id']} ({item['category_id']}), now "
                    f"{compact(item['current'])}: {actions}"
                )
            else:
                found.append(f"[overrides] option: {compact(item['value'])}")
        if message["next"] is not None:
            found.append("[overrides] More options exist: narrow with a match.")
        return found or ["[overrides] Nothing here."]
    if kind == "override_preview":
        return preview_lines(message["preview"], message["preview_digest"])
    if kind == "override_applied":
        return [
            f"[override] Applied to {message['character_id']} at revision "
            f"{message['revision']}: record {message['record_id']}."
        ]
    if kind == "override_requests":
        found = [request_line(r, role) for r in message["items"]]
        return found or ["[override] No override requests."]
    if kind == "override_detail":
        request = message["request"]
        found = [request_line(request, role)]
        if request.get("revises"):
            found.append(f"[override]   revises {request['revises']}")
        if request.get("decision_note"):
            found.append(f"[override]   note: {compact(request['decision_note'])}")
        found += preview_lines(request["preview"], request["preview_digest"],
                               "[asked]")  # fmt: skip
        current = request["current"]
        if current["status"] == "changed":
            found.append("[override] What it would do now has changed:")
            found += preview_lines(
                current["preview"], current["preview_digest"], "[now]"
            )
        elif current["status"] == "invalid":
            found.append("[override] It cannot be done now: "
                         + " ".join(current["problems"] or []))  # fmt: skip
        elif current["status"] == "not_checked":
            found.append(f"[override] Not checked now ({current['reason']}).")
        if message["offer"] is not None:
            found.append(offer_line(message["offer"]))
        return found
    if kind == "override_offers":
        return [offer_line(o) for o in message["items"]] or ["[offer] No offers yet."]
    if kind == "override_offer":
        offer = message["offer"]
        found = [offer_line(offer)]
        if offer.get("close_note"):
            found.append(f"[offer]   closing note: {compact(offer['close_note'])}")
        found += preview_lines(offer["preview"], offer["preview_digest"], "[offer]")
        return found
    if kind == "override_history":
        found = [
            f"[override] {r['record_id']} {r['kind']} at revision {r['revision']}: "
            f"{r['headline']}"
            for r in message["items"]
        ]
        return found or ["[override] Nothing applied yet."]
    if kind == "override_record":
        record = message["record"]
        found = [
            f"[override] {record['record_id']} {record['kind']} on "
            f"{record['character_id']} at revision {record['revision']}: "
            f"{record['headline']}"
        ]
        if record.get("reason"):
            found.append(f"[override]   reason: {compact(record['reason'])}")
        found.append(f"[override]   receipt: {compact(record['receipt'])}")
        return found
    return []


def error_lines(message: dict) -> list[str]:
    """What an override refusal carries, beyond its message."""
    code = message["code"]
    found = []
    if code == "invalid_intent":
        for problem in message.get("problems") or []:
            found.append(f"[override]   {problem}")
    elif code == "override_blocked":
        names = ", ".join(c.get("label", "?") for c in message.get("categories") or [])
        found.append(f"[override]   blocked here: {names}")
    elif code == "stale_preview" and isinstance(message.get("preview"), dict):
        found.append("[override] It would now do this instead (nothing was applied):")
        found += preview_lines(
            message["preview"], message.get("preview_digest"), "[now]"
        )
    elif code == "offer_stale":
        found.append(
            f"[override]   {message.get('proposal_id')} is {message.get('state')} "
            f"({message.get('stale_reason')}); nothing was applied."
        )
    return found


def _list(value, check) -> bool:
    return isinstance(value, list) and all(check(v) for v in value)


def _dict(value) -> bool:
    return isinstance(value, dict)


def _page(message, check) -> bool:
    return _list(message.get("items"), check) and (
        message.get("next") is None or isinstance(message["next"], str)
    )


def _preview(value) -> bool:
    return _dict(value) and isinstance(value.get("headline"), str) and all(
        isinstance(value.get(k), list)
        for k in ("effects", "waivers", "allowances", "later_steps", "consequences",
                  "limitations")
    )  # fmt: skip


def _request(value) -> bool:
    return (
        _dict(value)
        and isinstance(value.get("proposal_id"), str)
        and value.get("state") in OVERRIDE_STATES
        and _dict(value.get("target"))
        and _dict(value.get("requester"))
        and isinstance(value.get("explanation"), str)
        and isinstance(value.get("categories"), list)
        and (value.get("offer") is None or _offer(value["offer"]))
    )


def _offer(value) -> bool:
    return (
        _dict(value)
        and isinstance(value.get("offer_id"), str)
        and isinstance(value.get("offer_revision"), int)
        and isinstance(value.get("state"), str)
        and isinstance(value.get("headline"), str)
    )


# The shape each override reply must have before it is shown.
SHAPES = {
    "override_categories": lambda m: _list(m.get("categories"), _dict),
    "override_discover": lambda m: isinstance(m.get("character_id"), str)
    and _page(m, _dict),
    "override_preview": lambda m: _preview(m.get("preview"))
    and isinstance(m.get("preview_digest"), str),
    "override_applied": lambda m: isinstance(m.get("record_id"), str)
    and isinstance(m.get("revision"), int),
    "override_requests": lambda m: _page(m, _request),
    "override_detail": lambda m: _request(m.get("request"))
    and _preview(m["request"].get("preview")) and _dict(m["request"].get("current"))
    and (m.get("offer") is None or _offer(m["offer"])),
    "override_offers": lambda m: _page(m, _offer),
    "override_offer": lambda m: _offer(m.get("offer")) and _preview(m["offer"].get("preview")),
    "override_history": lambda m: _page(m, _dict),
    "override_record": lambda m: _dict(m.get("record")),
}  # fmt: skip
