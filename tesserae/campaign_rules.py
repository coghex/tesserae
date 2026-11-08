"""A campaign's settings documents: import, list, export, activate and
reject (docs/designs/governance_contract.md section 5), with each host
mutation's exact replay and audit (section 8).

A companion of GameStore, like tesserae.referee.store: every operation is one
transaction on the store's connection. Authority is the caller's to
establish (the lobby's guard for a campaign host, the local operator for the
`tesserae game rules` commands); this module records who decided.
"""

import json
from datetime import datetime, timezone

from . import policy as policies
from .protocol import PROTOCOL_VERSION
from .store import OPERATOR, OPERATOR_DECIDER, Decider, GameStore, Refusal

__all__ = ["OPERATOR", "OPERATOR_DECIDER", "CampaignRules", "Decider"]


class CampaignRules:
    def __init__(self, store: GameStore):
        self.store = store

    # --- Reads (inside the caller's snapshot) -------------------------------

    def revisions(self) -> list[dict]:
        """Every revision's metadata, oldest first; no content."""
        rows = self.store.query(
            "SELECT revision, state, origin, sha256, created_at, activated_at, "
            "activated_by FROM campaign_documents ORDER BY revision"
        )
        keys = ("revision", "state", "origin", "sha256", "created_at",
                "activated_at", "activated_by")  # fmt: skip
        return [dict(zip(keys, row)) for row in rows]

    def document(self, revision: int | None = None) -> dict:
        """`{"revision", "state", "content"}` of one revision, by default the
        active one. Raises Refusal if there is none."""
        if revision is None:
            rows = self.store.query(
                "SELECT revision, state, content FROM campaign_documents "
                "WHERE state = 'active'"
            )
            if not rows:
                raise Refusal(
                    "invalid_request", "No settings are active yet; name a revision."
                )
        else:
            rows = self.store.query(
                "SELECT revision, state, content FROM campaign_documents "
                "WHERE revision = ?",
                (revision,),
            )
            if not rows:
                raise Refusal("invalid_request", "There is no such revision.")
        number, state, content = rows[0]
        return {"revision": number, "state": state, "content": json.loads(content)}

    def projection(self, character_id: str | None = None) -> dict:
        """What any admitted player may read (section 7's `rules_view`): the
        campaign's settings and guidance, and for `character_id` (checked by
        the caller) its effective settings, its own exception and its
        requirements. Never another character's exception."""
        store, runtime = self.store, self.store.runtime()
        revision, document = store.active_document()
        state = store.inspect_state()
        view = {
            "policy_revision": revision,
            "settings": {},
            "guidance": None if document is None else document["guidance"],
            "requirements": None,
        }
        if runtime.creation_overrides is not None:
            policy = policies.category_policy(runtime, document)
            view["override_categories"] = [
                {"category_id": c["category_id"], "label": c["label"],
                 "policy": policy[c["category_id"]]}
                for c in runtime.override_categories()
            ]  # fmt: skip
        if runtime.settings is None:
            return view
        policy = policies.effective(runtime, revision, document, character_id)
        view["settings"] = policy["settings"]
        view["requirements"] = requirements(store, runtime, policy, state, character_id)
        if character_id is not None:
            view["character_id"] = character_id
            exception = policies.own_exception(document, character_id)
            view["exception"] = None if exception is None else {
                key: exception[key] for key in ("exception_id", "settings", "reason")
            }  # fmt: skip
        return view

    # --- Mutations ----------------------------------------------------------
    # Each returns (result, applied) and raises Refusal with nothing changed.
    # `check()`, if given, runs first inside the transaction: the caller's
    # authority, so a revocation is ordered against the change. With a
    # `request_id` the change replays exactly (section 8).

    def import_document(
        self, document, by: Decider, *, request_id=None, origin="import", check=None
    ) -> tuple[dict, bool]:
        """Store a valid document as a new candidate. Nothing live changes."""
        content = {"content": document}

        def apply(db):
            revision, sha = self.store_candidate(db, document, origin)
            self._audit(db, by, "import", {"revision": revision, "sha256": sha,
                                           "request_id": request_id})  # fmt: skip
            return {"revision": revision, "state": "candidate"}

        return self._mutation(by, request_id, "rules_import", content, apply, check)

    def activate(
        self, revision: int, expected_policy_revision: int | None, by: Decider,
        *, request_id=None, check=None,
    ) -> tuple[dict, bool]:  # fmt: skip
        """Make a candidate the active document, if the policy revision is
        still `expected_policy_revision` (None: the operator's, unchecked),
        no referee job is running, and the module accepts it against the
        game now. Drafts whose effective settings change get a new draft
        revision; completed characters are never touched."""
        content = {"revision": revision,
                   "expected_policy_revision": expected_policy_revision}  # fmt: skip

        def apply(db):
            row = self._candidate(db, revision, "activated")
            current, old = self.store.active_document()
            if (
                expected_policy_revision is not None
                and expected_policy_revision != current
            ):
                raise Refusal(
                    "stale_policy",
                    f"The campaign's settings are at revision {current}, not "
                    f"{expected_policy_revision}; review them again.",
                    {"policy_revision": current},
                )
            busy = self.store.busy_refusal(by.job_id)
            if busy is not None:
                raise busy
            bumped = self.make_active(db, revision, json.loads(row[0]), by)
            self._audit(db, by, "activation", {
                "revision": revision, "previous": current, "drafts": bumped,
                "request_id": request_id,
            })  # fmt: skip
            return {"revision": revision, "state": "active"}

        return self._mutation(by, request_id, "rules_activate", content, apply, check)

    # --- Inside a caller's transaction (also an approved request's) ---------

    def store_candidate(self, db, document, origin: str) -> tuple[int, str]:
        """Check a document whole against the game now and store it as a new
        candidate: (revision, sha256). Raises Refusal (`invalid_document`)."""
        runtime = self.store.runtime()
        binding = self.store.binding()
        checked = _checked(
            document,
            {"id": binding.module_id, "version": binding.version,
             "state_version": binding.state_version},
            runtime,
        )  # fmt: skip
        problems = policies.check_against(
            checked, runtime, self.store.inspect_state(), self._character_ids()
        )
        if problems:
            raise _invalid(problems)
        text = policies.canonical(checked)
        sha = policies.digest(text)
        revision = (
            1
            + db.execute(
                "SELECT ifnull(max(revision), 0) FROM campaign_documents"
            ).fetchone()[0]
        )
        db.execute(
            "INSERT INTO campaign_documents (revision, content, sha256, origin, "
            "state, created_at) VALUES (?, ?, ?, ?, 'candidate', ?)",
            (revision, text, sha, origin, _now()),
        )
        return revision, sha

    def make_active(self, db, revision: int, document: dict, by: Decider) -> list:
        """Check a stored candidate against the game now and make it the
        active document: the previous one superseded, and every draft whose
        effective settings change given a new draft revision. The drafts
        bumped. Raises Refusal (`invalid_document`)."""
        current, old = self.store.active_document()
        runtime = self.store.runtime()
        problems = policies.check_against(
            document, runtime, self.store.inspect_state(), self._character_ids()
        )
        if problems:
            raise _invalid(problems)
        bumped = []
        for (character_id,) in db.execute(
            "SELECT character_id FROM characters WHERE phase = 'draft' "
            "ORDER BY created_at, rowid"
        ).fetchall():
            before = policies.effective(runtime, current, old, character_id)
            after = policies.effective(runtime, revision, document, character_id)
            # As JSON, not Python values: false is not 0, nor 1.0 1.
            if policies.canonical(before["settings"]) != policies.canonical(
                after["settings"]
            ):
                bumped.append(character_id)
        db.executemany(
            "UPDATE characters SET draft_revision = draft_revision + 1 "
            "WHERE character_id = ?",
            [(character_id,) for character_id in bumped],
        )
        db.execute(
            "UPDATE campaign_documents SET state = 'superseded' WHERE state = 'active'"
        )
        db.execute(
            "UPDATE campaign_documents SET state = 'active', activated_at = ?, "
            "activated_by = ? WHERE revision = ?",
            (_now(), by.player_id, revision),
        )
        # A draft whose effective settings changed re-checks its open offer
        # (creation overrides contract section 7); policy alone never stales.
        for character_id in bumped:
            self.store.recheck_offers(character_id)
        return bumped

    def reject(
        self, revision: int, by: Decider, *, request_id=None, check=None
    ) -> tuple[dict, bool]:
        """A candidate becomes rejected; nothing live changes."""
        content = {"revision": revision}

        def apply(db):
            self._candidate(db, revision, "rejected")
            db.execute(
                "UPDATE campaign_documents SET state = 'rejected' WHERE revision = ?",
                (revision,),
            )
            self._audit(db, by, "rejection", {"revision": revision,
                                              "request_id": request_id})  # fmt: skip
            return {"revision": revision, "state": "rejected"}

        return self._mutation(by, request_id, "rules_reject", content, apply, check)

    # --- Internals ----------------------------------------------------------

    def _mutation(self, by, request_id, kind, content, apply, check):
        return mutation(self.store, by.sender, request_id, kind, content, apply, check)

    def _candidate(self, db, revision, verb: str):
        row = db.execute(
            "SELECT content, state FROM campaign_documents WHERE revision = ?",
            (revision,),
        ).fetchone()
        if row is None:
            raise Refusal("invalid_request", "There is no such revision.")
        if row[1] != "candidate":
            raise Refusal(
                "invalid_request",
                f"Revision {revision} is {row[1]}; only a candidate can be {verb}.",
            )
        return row

    def _character_ids(self) -> frozenset:
        return frozenset(
            row[0] for row in self.store.query("SELECT character_id FROM characters")
        )

    def _audit(self, db, by: Decider, kind: str, detail: dict):
        self.store.audit(by, kind, detail)


def mutation(store: GameStore, sender: str, request_id, kind, content, apply, check):
    """One host mutation (governance contract section 8): in one
    transaction, `check()` (the caller's authority), then the exact replay of
    `sender`'s `request_id` (the recorded reply, or `request_conflict` for
    other content), else `apply(db)` and its reply recorded with it.
    Returns (reply, applied); a Refusal changes nothing and records
    nothing."""
    recorded = policies.canonical({"kind": kind, **content})
    with store.transaction() as db:
        if check is not None:
            check()
        if request_id is not None:
            row = db.execute(
                "SELECT content, result FROM host_mutations "
                "WHERE sender = ? AND request_id = ?",
                (sender, request_id),
            ).fetchone()
            if row is None and store.recorded_command(sender, request_id):
                # One namespace spans commands and host mutations.
                raise Refusal(
                    "request_conflict",
                    "This request_id was already used for a different request.",
                )
            if row is not None:
                if row[0] != recorded:
                    raise Refusal(
                        "request_conflict",
                        "This request_id was already used for a different request.",
                    )
                return json.loads(row[1]), False
        result = apply(db)
        if request_id is not None:
            db.execute(
                "INSERT INTO host_mutations (sender, request_id, kind, content, "
                "result, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (sender, request_id, kind, recorded, json.dumps(result), _now()),
            )
    return result, True


def requirements(store: GameStore, runtime, policy, state, character_id) -> list | None:
    """The module's requirements for a character (or a new one, with None),
    after the host's own: a character game's name. None without the
    capability."""
    entries = runtime.requirements(policy, state, character_id)
    if entries is None or runtime.characters is None:
        return entries
    name = {
        "id": "character.name",
        "kind": "mechanical",
        "description": "A name for the character, any text you like; it must "
        "have one before it can be approved.",
        "method": "choose",
    }
    if character_id is not None:
        character = store.character(character_id)
        name["met"] = character is not None and character.name is not None
    return [name, *entries]


def rules_message(result: dict, request_id: str | None = None) -> dict:
    """`rules {revision, state}` (and `content` for an export)."""
    message = {"version": PROTOCOL_VERSION, "type": "rules", **result}
    if request_id is not None:
        message["request_id"] = request_id
    return message


def _checked(document, binding: dict, runtime) -> dict:
    try:
        categories = None
        if runtime.creation_overrides is not None:
            categories = frozenset(
                c["category_id"] for c in runtime.override_categories()
            )
        return policies.check_shape(
            document, binding, frozenset(runtime.settings_defaults()), categories
        )
    except policies.DocumentError as error:
        raise _invalid(error.problems) from None


def _invalid(problems: list) -> Refusal:
    return Refusal(
        "invalid_document",
        "That settings document cannot be used; nothing was changed.",
        {"problems": list(problems)},
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
