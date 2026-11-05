"""Durable referee state: input, jobs, the busy gate, tool steps, pending
choices, messages, rulings and the Codex session (#65, REF-2).

In a character game (docs/designs/character_contract.md sections 6 to 8),
every input keeps the actor it was accepted for: its character and mode.
Its job acts, asks and narrates only within that actor's scope, and a
creation exchange stays private to its player.

Everything lives in the game's own SQLite file (schema 5) and is written in
`GameStore.transaction()`, the same serialized write lock as gameplay. The
mechanical revision never moves for conversation; messages have their own
sequence. This module runs no model process and no network; see
docs/designs/referee_contract.md sections 5 to 7 and 10.
"""

import json
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from ..protocol import is_valid_request_id
from ..store import (
    REFEREE_PARTICIPANT,
    SETUP,
    Actor,
    GameStore,
    ParticipantContext,
    StoreError,
    is_character_id,
)

REFEREE = REFEREE_PARTICIPANT  # the context for referee-only commands
MAX_TEXT = 2000
MAX_MESSAGE = 4000
MAX_MESSAGES = 8
MAX_CHOICES = 4
MAX_QUESTION = 1000
TRANSCRIPT_PAGE = 200
# A page's encoded size, so a page fits in one tool result Codex shows the
# model whole (it trims large tool outputs). A longer message still gets a
# page of its own.
TRANSCRIPT_PAGE_BYTES = 32 * 1024
MAX_NARRATED = 16
MAX_STEP_ID = 64  # so `ref:<job>:<step>` stays a valid request ID
FINISHED = ("completed", "failed", "interrupted")


class RefereeError(StoreError):
    """A referee request that cannot be accepted; nothing was changed. `code`
    is the wire error code (contract section 7)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Job:
    job_id: str
    participant_id: str
    request_id: str
    text: str
    private: bool
    state: str
    host_run: str | None
    revision_at_start: int
    error: str | None
    narrated: bool
    # The frozen actor, in a character game only: the character (None in
    # setup) and the mode. Both stay None in a game without characters.
    character_id: str | None = None
    mode: str | None = None

    @property
    def actor(self) -> Actor | None:
        return None if self.mode is None else Actor(self.character_id, self.mode)


@dataclass(frozen=True)
class Submission:
    """What `submit` decided: `accepted` (a new running job) or `replayed` (an
    input already recorded under that request ID), with its job and, once
    the job has finished, the messages addressed to the sender."""

    outcome: str
    job: Job
    messages: tuple


@dataclass(frozen=True)
class Step:
    job_id: str
    step_id: str
    kind: str
    context: str
    command: str
    args: dict
    request_id: str
    status: str
    result: dict | None
    # The actor its job was accepted for, in a character game: the character
    # it acted as (None for referee_act and in setup) and the job's mode.
    character_id: str | None = None
    mode: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_text(text, limit: int, what: str):
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > limit
        or any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text)
    ):
        raise RefereeError(
            "invalid_request",
            f"{what} is 1 to {limit} characters, not blank, with no control characters.",
        )


class RefereeStore:
    """The referee half of one game store. Use it on the thread that owns the
    GameStore; every method is one short transaction or read."""

    def __init__(self, store: GameStore):
        self.store = store

    # --- mode and session ------------------------------------------------------

    def enable(self):
        """Turn referee mode on for this game. It stays on."""
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO referee (singleton, enabled, session_id, state) "
                "VALUES (1, 1, NULL, 'ok') ON CONFLICT (singleton) DO UPDATE SET enabled = 1"
            )

    def enabled(self) -> bool:
        rows = self.store.query("SELECT enabled FROM referee")
        return bool(rows and rows[0][0])

    def session(self) -> tuple[str | None, str]:
        """(session ID or None, state: `ok` or `needs_rebuild`)."""
        rows = self.store.query("SELECT session_id, state FROM referee")
        if not rows:
            raise RefereeError(
                "referee_off", "Referee mode is not enabled for this game."
            )
        return rows[0]

    def set_session(self, session_id: str):
        """Record the Codex session a job started (contract section 4)."""
        _check_text(session_id, 200, "A session ID")
        with self.store.transaction() as db:
            current = db.execute("SELECT session_id FROM referee").fetchone()
            if current is None:
                raise RefereeError(
                    "referee_off", "Referee mode is not enabled for this game."
                )
            if current[0] not in (None, session_id):
                raise RefereeError(
                    "session_conflict", "This game already has a referee session."
                )
            db.execute("UPDATE referee SET session_id = ?", (session_id,))
            db.execute(
                "INSERT INTO referee_sessions (session_id, started_at) VALUES (?, ?) "
                "ON CONFLICT (session_id) DO NOTHING",
                (session_id, _now()),
            )

    def mark_needs_rebuild(self):
        with self.store.transaction() as db:
            db.execute("UPDATE referee SET state = 'needs_rebuild'")

    def rebuild(self):
        """End the current session and clear it; the next job starts a new one
        from the stored transcript. Game state is not touched."""
        with self.store.transaction() as db:
            if self._running(db) is not None:
                raise RefereeError("referee_busy", "A referee job is still running.")
            db.execute(
                "UPDATE referee_sessions SET ended_at = ?, ended_reason = 'rebuild' "
                "WHERE session_id = (SELECT session_id FROM referee) AND ended_at IS NULL",
                (_now(),),
            )
            db.execute("UPDATE referee SET session_id = NULL, state = 'ok'")

    def sessions(self) -> list[tuple]:
        return self.store.query(
            "SELECT session_id, started_at, ended_at, ended_reason "
            "FROM referee_sessions ORDER BY started_at, session_id"
        )

    # --- input and the busy gate (contract section 5) ---------------------------

    def submit(self, context: ParticipantContext, request_id, text, private: bool,
               host_run: str | None = None, *, actor: Actor | None = None) -> Submission:  # fmt: skip
        """Accept one player's input, replay an earlier one, or refuse.

        In one transaction: the context's credential must still be active
        (`session_revoked` otherwise); an exact retry (same participant and request ID,
        same text and privacy) returns its recorded job, busy or not; another
        content under that ID is `request_conflict`; otherwise a running job
        means `referee_busy`, with nothing stored; otherwise the input, the
        player's own message and a running job are stored together.

        In a character game the input is accepted for `actor` (setup when
        omitted), which is frozen with it and part of its replay content. A
        creation input is private whatever `private` says. An actor whose
        character is no longer the sender's, or whose phase has moved on, is
        refused (`character_unavailable`, `character_changed`) after replay
        and before the busy check. A game without characters takes no actor.
        """
        if (
            not isinstance(context, ParticipantContext)
            or context.participant_id == REFEREE
        ):
            raise TypeError("A trusted player ParticipantContext is required.")
        if not is_valid_request_id(request_id):
            raise RefereeError(
                "invalid_request",
                "A request ID is 1 to 128 characters with no whitespace.",
            )
        _check_text(text, MAX_TEXT, "Player text")
        if not isinstance(private, bool):
            raise RefereeError("invalid_request", "'private' is true or false.")
        actor = self._actor_for(actor)
        if actor is not None and actor.mode == "creation":
            private = True  # creation is private, enforced here (contract section 8)
        character_id = None if actor is None else actor.character_id
        mode = None if actor is None else actor.mode
        who = context.participant_id
        with self.store.transaction() as db:
            # Inside the write lock, before any replay or new job, as for
            # gameplay commands: a revoked or rotated credential gets nothing.
            if self.store.session_revision(context) is None:
                raise RefereeError(
                    "session_revoked", "This session's credential is no longer valid."
                )
            if db.execute("SELECT enabled FROM referee").fetchone() is None:
                raise RefereeError(
                    "referee_off", "Referee mode is not enabled for this game."
                )
            earlier = db.execute(
                "SELECT text, private, character_id, mode FROM referee_inputs "
                "WHERE participant_id = ? AND request_id = ?",
                (who, request_id),
            ).fetchone()
            if earlier is not None:
                if (earlier[0], bool(earlier[1]), *earlier[2:]) != (
                    text, private, character_id, mode,
                ):  # fmt: skip
                    raise RefereeError(
                        "request_conflict",
                        "That request ID was already used for other input.",
                    )
                job = self._job_for_input(db, who, request_id)
                return Submission("replayed", job, self._reply_messages(db, job))
            problem = (
                None if actor is None else self.store.actor_problem(context, actor)
            )
            if problem is not None:
                raise RefereeError(*problem)
            if self._running(db) is not None:
                raise RefereeError(
                    "referee_busy", "The referee is still answering. Try again soon."
                )
            if db.execute("SELECT state FROM referee").fetchone()[0] == "needs_rebuild":
                raise RefereeError(
                    "referee_needs_rebuild",
                    "The referee's session is lost; the host must rebuild it.",
                )
            now = _now()
            input_id = db.execute(
                "INSERT INTO referee_inputs (participant_id, request_id, text, private, "
                "received_at, character_id, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (who, request_id, text, int(private), now, character_id, mode),
            ).lastrowid
            job_id = "j-" + secrets.token_hex(6)
            revision = db.execute("SELECT revision FROM game").fetchone()[0]
            db.execute(
                "INSERT INTO referee_jobs (job_id, input_id, state, host_run, revision_at_start, "
                "started_at) VALUES (?, ?, 'running', ?, ?, ?)",
                (job_id, input_id, host_run, revision, now),
            )
            audience = [who] if private else "table"
            db.execute(
                "INSERT INTO referee_messages (job_id, sender, audience, text, created_at, "
                "character_id, creation) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, who, json.dumps(audience), text, now, character_id,
                 int(mode == "creation")),
            )  # fmt: skip
            return Submission("accepted", self._job(db, job_id), ())

    def _actor_for(self, actor: Actor | None) -> Actor | None:
        """The actor an input or read is for: `actor`, or setup when omitted,
        in a character game; always None in a game without characters."""
        if self.store.runtime().characters is None:
            if actor is not None:
                raise ValueError("Only a character game's inputs have an actor.")
            return None
        if actor is not None and not isinstance(actor, Actor):
            raise TypeError("A trusted Actor is required.")
        return SETUP if actor is None else actor

    def running_job(self) -> Job | None:
        return self._running(self.store)

    def job(self, job_id: str) -> Job:
        return self._job(self.store, job_id)

    def previous_job(self, job_id: str) -> Job | None:
        """The job accepted just before this one, if any."""
        rows = self._rows(
            self.store,
            self._JOB_SELECT + "WHERE i.id < (SELECT input_id FROM referee_jobs "
            "WHERE job_id = ?) ORDER BY i.id DESC LIMIT 1",
            (job_id,),
        )
        return _job_from(rows[0]) if rows else None

    def finish(self, job_id: str, state: str, error: str | None = None) -> Job:
        """End a running job without a published reply: `failed` or
        `interrupted` (contract section 10). Its committed steps stay listed
        for later jobs until a reply narrates them."""
        if state not in ("failed", "interrupted"):
            raise ValueError(
                "finish() is for failed or interrupted jobs; use publish_reply()."
            )
        with self.store.transaction() as db:
            self._require_running(db, job_id)
            db.execute(
                "UPDATE referee_jobs SET state = ?, ended_at = ?, error = ? WHERE job_id = ?",
                (state, _now(), error, job_id),
            )
            return self._job(db, job_id)

    # --- the reply (contract section 7) ----------------------------------------

    def publish_reply(self, job_id: str, reply: dict) -> tuple:
        """Validate a running job's reply, then in one transaction store its
        messages, apply its choices, mark the steps it narrates and complete
        the job. Returns the stored messages. A reply that does not validate
        raises RefereeError (`malformed_reply`) and changes nothing.

        A reply narrates this job's own committed steps, and the earlier
        unnarrated steps it lists in `narrated`. Each listed step's sender must
        be able to see at least one of the reply's messages: a result is only
        acknowledged once it reaches the player it belongs to.
        """
        with self.store.transaction() as db:
            job = self._require_running(db, job_id)
            participants = {
                row[0] for row in db.execute("SELECT participant_id FROM participants")
            }
            scope = None if job.mode is None else self._scope_of(db, job)
            messages, asks, resolves = _validated_reply(reply, job, participants, scope)
            acknowledged = self._acknowledged(db, job, reply["narrated"], messages)
            now = _now()
            creation = int(job.mode == "creation")
            stored = []
            for audience, text in messages:
                seq = db.execute(
                    "INSERT INTO referee_messages (job_id, sender, audience, text, "
                    "created_at, character_id, creation) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, REFEREE, json.dumps(audience), text, now,
                     job.character_id, creation),
                ).lastrowid  # fmt: skip
                stored.append(
                    _message(seq, REFEREE, audience, text, job.character_id, creation)
                )
            # Each resolve and ask names one scope: (participant, character),
            # where a null character is the participant's own setup question.
            # A game without characters resolves participant-wide, as before.
            for participant_id, character_id in resolves:
                if job.mode is None:
                    db.execute(
                        "DELETE FROM referee_choices WHERE participant_id = ?",
                        (participant_id,),
                    )
                else:
                    db.execute(
                        "DELETE FROM referee_choices WHERE participant_id = ? "
                        "AND character_id IS ?",
                        (participant_id, character_id),
                    )
            for (participant_id, character_id), question in asks:
                db.execute(  # asking again replaces that scope's question
                    "DELETE FROM referee_choices WHERE participant_id = ? "
                    "AND character_id IS ?",
                    (participant_id, character_id),
                )
                db.execute(
                    "INSERT INTO referee_choices (participant_id, character_id, question, "
                    "asked_in_job, asked_at) VALUES (?, ?, ?, ?, ?)",
                    (participant_id, character_id, question, job_id, now),
                )
            if job.mode == "play":  # the character is now in the scene
                db.execute(
                    "UPDATE characters SET introduced_at = ? WHERE character_id = ? "
                    "AND introduced_at IS NULL",
                    (now, job.character_id),
                )
            db.execute(
                "UPDATE referee_steps SET narrated = 1 WHERE job_id = ? AND status = 'committed'",
                (job_id,),
            )
            db.executemany(
                "UPDATE referee_steps SET narrated = 1 WHERE job_id = ? AND step_id = ?",
                acknowledged,
            )
            db.execute(
                "UPDATE referee_jobs SET state = 'completed', ended_at = ?, narrated = 1 "
                "WHERE job_id = ?",
                (now, job_id),
            )
            return tuple(stored)

    # --- tool steps and their recovery boundary (contract section 6) -----------

    def intend_step(self, job_id: str, step_id, kind: str, context: str, command: str,
                    args: dict) -> Step:  # fmt: skip
        """Journal a step before it runs. The same step ID with the same
        kind, context, command and arguments returns the existing row; any
        difference is `request_conflict`, across both kinds of step. A
        player step records the job's frozen character."""
        if not is_valid_request_id(step_id) or len(step_id) > MAX_STEP_ID:
            raise RefereeError(
                "invalid_request",
                f"A step ID is 1 to {MAX_STEP_ID} characters with no whitespace.",
            )
        if kind not in ("act", "referee_act"):
            raise ValueError(f"Unknown step kind {kind!r}.")
        if (kind == "referee_act") != (context == REFEREE):
            raise ValueError(
                "referee_act runs in the referee context, and act in a player's."
            )
        encoded = json.dumps(args, sort_keys=True)
        with self.store.transaction() as db:
            job = self._require_running(db, job_id)
            existing = db.execute(
                "SELECT kind, context, command, args FROM referee_steps "
                "WHERE job_id = ? AND step_id = ?",
                (job_id, step_id),
            ).fetchone()
            if existing is not None:
                if existing != (kind, context, command, encoded):
                    raise RefereeError(
                        "request_conflict",
                        "That step ID was already used for another step.",
                    )
            else:
                db.execute(
                    "INSERT INTO referee_steps (job_id, step_id, kind, context, command, args, "
                    "request_id, status, character_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'intended', ?)",
                    (
                        job_id,
                        step_id,
                        kind,
                        context,
                        command,
                        encoded,
                        f"ref:{job_id}:{step_id}",
                        job.character_id if kind == "act" else None,
                    ),
                )
            return self._step(db, job_id, step_id)

    def record_step(self, job_id: str, step_id: str, status: str, result: dict | None):
        """After running a step: `committed` with the store's result message,
        or `refused` with its error message (the same shapes exact replay and
        `reconcile_steps` give)."""
        if status not in ("committed", "refused"):
            raise ValueError("A recorded step is committed or refused.")
        with self.store.transaction() as db:
            db.execute(
                "UPDATE referee_steps SET status = ?, result = ? "
                "WHERE job_id = ? AND step_id = ? AND status = 'intended'",
                (status, json.dumps(result, sort_keys=True), job_id, step_id),
            )

    def reconcile_steps(self) -> list[Step]:
        """Settle every `intended` step: if its mapped request ID is in the
        acting context's command history it committed (with that result);
        otherwise it never ran. Call at startup and before any new job."""
        settled = []
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT job_id, step_id, context, request_id FROM referee_steps "
                "WHERE status = 'intended'"
            ).fetchall()
            for job_id, step_id, context, request_id in rows:
                # The same reply the store's exact replay gives (record_step's shape).
                found = self.store.lookup(ParticipantContext(context), request_id)
                if found is None:
                    db.execute(
                        "UPDATE referee_steps SET status = 'not_committed' WHERE job_id = ? AND step_id = ?",
                        (job_id, step_id),
                    )
                else:
                    db.execute(
                        "UPDATE referee_steps SET status = 'committed', result = ? "
                        "WHERE job_id = ? AND step_id = ?",
                        (
                            json.dumps(found.to_message(), sort_keys=True),
                            job_id,
                            step_id,
                        ),
                    )
                settled.append(self._step(db, job_id, step_id))
        return settled

    def unnarrated_steps(self, for_job: Job | None = None) -> list[Step]:
        """Committed steps of finished jobs that no published reply has
        narrated yet, oldest first (for `get_context`). For a job of a
        character game, only those it may narrate (contract section 8): a
        creation job sees its own draft's creation results, and any other
        job sees every result except creation results."""
        clause, parameters = "", ()
        if for_job is not None and for_job.mode is not None:
            clause, parameters = _results_for(for_job)
        rows = self.store.query(
            "SELECT s.job_id, s.step_id FROM referee_steps AS s JOIN referee_jobs AS j "
            "ON j.job_id = s.job_id JOIN referee_inputs AS i ON i.id = j.input_id "
            "WHERE s.status = 'committed' AND s.narrated = 0 AND j.state != 'running'"
            f"{clause} ORDER BY j.started_at, s.rowid",
            parameters,
        )
        return [self._step(self.store, job_id, step_id) for job_id, step_id in rows]

    # --- rulings -------------------------------------------------------------------

    def record_ruling(self, job_id: str, text, scope):
        _check_text(text, MAX_MESSAGE, "A ruling")
        _check_text(scope, MAX_QUESTION, "A ruling's scope")
        with self.store.transaction() as db:
            self._require_running(db, job_id)
            db.execute(
                "INSERT INTO referee_rulings (job_id, text, scope, created_at) VALUES (?, ?, ?, ?)",
                (job_id, text, scope, _now()),
            )

    def rulings(self) -> list[dict]:
        return [
            {"id": i, "job_id": j, "text": t, "scope": s, "created_at": c}
            for i, j, t, s, c in self.store.query(
                "SELECT id, job_id, text, scope, created_at FROM referee_rulings ORDER BY id"
            )
        ]

    # --- reads by audience -------------------------------------------------------

    def messages_for(self, participant_id: str, after_seq: int = 0) -> list[dict]:
        """Messages this participant may see: the table's and their own private
        ones. Never another participant's private messages."""
        rows = self.store.query(
            f"SELECT {_MESSAGE_COLUMNS} FROM referee_messages WHERE seq > ? ORDER BY seq",
            (after_seq,),
        )
        visible = []
        for row in rows:
            message = _message_from(row)
            if message["audience"] == "table" or participant_id in message["audience"]:
                visible.append(message)
        return visible

    def input_message(self, job_id: str) -> dict:
        """The message holding a job's input: the player's own words."""
        [row] = self.store.query(
            f"SELECT {_MESSAGE_COLUMNS} FROM referee_messages "
            "WHERE job_id = ? AND sender != ? ORDER BY seq LIMIT 1",
            (job_id, REFEREE),
        )
        return _message_from(row)

    def transcript(self, after_seq: int = 0) -> tuple[list[dict], int | None]:
        """Referee only: the complete conversation, one page at a time, and the
        `after_seq` of the next page (None at the end). Never truncated."""
        rows = self.store.query(
            f"SELECT {_MESSAGE_COLUMNS} FROM referee_messages WHERE seq > ? "
            "ORDER BY seq LIMIT ?",
            (after_seq, TRANSCRIPT_PAGE + 1),
        )
        page, size = [], 0
        for row in rows:
            message = _message_from(row)
            size += len(json.dumps(message))
            if page and (len(page) == TRANSCRIPT_PAGE or size > TRANSCRIPT_PAGE_BYTES):
                return page, page[-1]["seq"]
            page.append(message)
        return page, None

    def all_choices(self, for_job: Job | None = None, *, every=False) -> list[dict]:
        """Referee only: every pending choice. For a job of a character game,
        each names its character (None for a setup question), and another
        draft's question is left out (contract section 7). With `every`, for
        the campaign host: all of them, each naming its character."""
        rows = self.store.query(
            "SELECT r.participant_id, r.question, r.asked_in_job, r.character_id, "
            "c.phase FROM referee_choices AS r LEFT JOIN characters AS c "
            "ON c.character_id = r.character_id ORDER BY r.asked_at, r.participant_id"
        )
        if every:
            return [
                {
                    "participant_id": p,
                    "character_id": c,
                    "question": q,
                    "asked_in_job": j,
                }
                for p, q, j, c, _ in rows
            ]
        if for_job is None or for_job.mode is None:
            return [
                {"participant_id": p, "question": q, "asked_in_job": j}
                for p, q, j, _, _ in rows
            ]
        return [
            {"participant_id": p, "character_id": c, "question": q, "asked_in_job": j}
            for p, q, j, c, phase in rows
            if phase != "draft" or c == for_job.character_id
        ]

    def projection(self, participant_id: str, actor: Actor | None = None) -> dict:
        """The recipient-scoped referee object for status, join and sync
        (contract section 7): busy or idle, only the viewer's own pending
        choice, the last message seq they can see, and their last input, all
        read from one committed state. In a character game the choice and
        the last input are those of `actor` (setup when omitted) only."""
        actor = self._actor_for(actor)
        with self.store.snapshot():
            return self._projection(participant_id, actor)

    def _projection(self, participant_id: str, actor: Actor | None) -> dict:
        if actor is None:
            choice = self.store.query(
                "SELECT question FROM referee_choices WHERE participant_id = ?",
                (participant_id,),
            )
            scope, parameters = "", ()
        else:
            choice = self.store.query(
                "SELECT question FROM referee_choices WHERE participant_id = ? "
                "AND character_id IS ?",
                (participant_id, actor.character_id),
            )
            scope = " AND i.character_id IS ? AND i.mode = ?"
            parameters = (actor.character_id, actor.mode)
        visible = self.messages_for(participant_id)
        last = self.store.query(
            "SELECT i.request_id, i.text, i.private, j.job_id, j.state FROM referee_inputs AS i "
            f"JOIN referee_jobs AS j ON j.input_id = i.id WHERE i.participant_id = ?{scope} "
            "ORDER BY i.id DESC LIMIT 1",
            (participant_id, *parameters),
        )
        return {
            "state": "busy" if self.running_job() is not None else "idle",
            "choices": [{"question": choice[0][0]}] if choice else [],
            "last_seq": visible[-1]["seq"] if visible else 0,
            "your_last_input": None if not last else {
                "request_id": last[0][0], "text": last[0][1], "private": bool(last[0][2]),
                "job_id": last[0][3], "job_state": last[0][4],
            },
        }  # fmt: skip

    # --- internals ---------------------------------------------------------------

    _JOB_SELECT = (
        "SELECT j.job_id, i.participant_id, i.request_id, i.text, i.private, j.state, "
        "j.host_run, j.revision_at_start, j.error, j.narrated, i.character_id, i.mode "
        "FROM referee_jobs AS j JOIN referee_inputs AS i ON i.id = j.input_id "
    )

    @staticmethod
    def _rows(db, sql, parameters=()):
        return (
            db.query(sql, parameters)
            if isinstance(db, GameStore)
            else db.execute(sql, parameters).fetchall()
        )

    def _job(self, db, job_id: str) -> Job:
        rows = self._rows(db, self._JOB_SELECT + "WHERE j.job_id = ?", (job_id,))
        if not rows:
            raise RefereeError("unknown_job", f"No referee job {job_id!r}.")
        return _job_from(rows[0])

    def _running(self, db) -> Job | None:
        rows = self._rows(db, self._JOB_SELECT + "WHERE j.state = 'running'")
        return _job_from(rows[0]) if rows else None

    def _require_running(self, db, job_id: str) -> Job:
        job = self._job(db, job_id)
        if job.state != "running":
            raise RefereeError(
                "job_not_running", f"Referee job {job_id} is {job.state}."
            )
        return job

    def _job_for_input(self, db, participant_id: str, request_id: str) -> Job:
        rows = self._rows(
            db, self._JOB_SELECT + "WHERE i.participant_id = ? AND i.request_id = ?",
            (participant_id, request_id),
        )  # fmt: skip
        return _job_from(rows[0])

    def _reply_messages(self, db, job: Job) -> tuple:
        """A finished job's referee messages that its sender may see."""
        if job.state not in FINISHED:
            return ()
        rows = self._rows(
            db,
            f"SELECT {_MESSAGE_COLUMNS} FROM referee_messages "
            "WHERE job_id = ? AND sender = ? ORDER BY seq",
            (job.job_id, REFEREE),
        )
        messages = (_message_from(row) for row in rows)
        return tuple(
            m
            for m in messages
            if m["audience"] == "table" or job.participant_id in m["audience"]
        )

    def _scope_of(self, db, job: Job) -> dict:
        """Every character's (owner, phase), for checking whom a character
        game's reply may address."""
        return {
            character_id: (owner, phase)
            for character_id, owner, phase in db.execute(
                "SELECT character_id, participant_id, phase FROM characters"
            )
        }

    def _acknowledged(self, db, job: Job, narrated, messages) -> list[tuple]:
        """The (job, step) pairs a reply's `narrated` list may acknowledge.
        Listing one of this job's own committed steps is allowed and changes
        nothing (they are narrated anyway); one that did not commit is not.
        In a character game only results this job may narrate count
        (`unnarrated_steps`): never a creation result outside its draft's
        creation jobs."""
        job_id = job.job_id
        clause, parameters = ("", ()) if job.mode is None else _results_for(job)
        pairs = []
        for entry in narrated:
            if entry["job_id"] == job_id:
                if (
                    db.execute(
                        "SELECT 1 FROM referee_steps WHERE job_id = ? AND step_id = ? "
                        "AND status = 'committed'",
                        (job_id, entry["step_id"]),
                    ).fetchone()
                    is None
                ):
                    raise RefereeError(
                        "malformed_reply",
                        "The reply lists a step of its own that did not commit.",
                    )
                continue
            row = db.execute(
                "SELECT i.participant_id FROM referee_steps AS s "
                "JOIN referee_jobs AS j ON j.job_id = s.job_id "
                "JOIN referee_inputs AS i ON i.id = j.input_id "
                "WHERE s.job_id = ? AND s.step_id = ? AND s.status = 'committed' "
                f"AND s.narrated = 0 AND j.state != 'running'{clause}",
                (entry["job_id"], entry["step_id"], *parameters),
            ).fetchone()
            if row is None:
                raise RefereeError(
                    "malformed_reply",
                    "The reply lists a step that is not an unnarrated earlier result.",
                )
            if not any(target == "table" or row[0] in target for target, _ in messages):
                raise RefereeError(
                    "malformed_reply",
                    "The reply acknowledges a result its player cannot see in any message.",
                )
            pairs.append((entry["job_id"], entry["step_id"]))
        return pairs

    def _step(self, db, job_id: str, step_id: str) -> Step:
        row = self._rows(
            db,
            "SELECT s.job_id, s.step_id, s.kind, s.context, s.command, s.args, "
            "s.request_id, s.status, s.result, s.character_id, i.mode "
            "FROM referee_steps AS s JOIN referee_jobs AS j ON j.job_id = s.job_id "
            "JOIN referee_inputs AS i ON i.id = j.input_id "
            "WHERE s.job_id = ? AND s.step_id = ?",
            (job_id, step_id),
        )[0]
        return Step(
            *row[:5],
            json.loads(row[5]),
            row[6],
            row[7],
            None if row[8] is None else json.loads(row[8]),
            row[9],
            row[10],
        )


def _job_from(row) -> Job:
    return Job(
        row[0],
        row[1],
        row[2],
        row[3],
        bool(row[4]),
        row[5],
        row[6],
        row[7],
        row[8],
        bool(row[9]),
        row[10],
        row[11],
    )


_MESSAGE_COLUMNS = "seq, sender, audience, text, character_id, creation"


def _message(seq: int, sender: str, audience, text: str, character_id=None,
             creation=False) -> dict:  # fmt: skip
    """A stored message. In a character game it also names its character
    and whether it belongs to a creation exchange (always private)."""
    message = {"seq": seq, "sender": sender, "audience": audience, "text": text}
    if character_id is not None or creation:
        message.update(character_id=character_id, creation=bool(creation))
    return message


def _message_from(row) -> dict:
    seq, sender, audience, text, character_id, creation = row
    return _message(seq, sender, json.loads(audience), text, character_id, creation)


def _results_for(job: Job) -> tuple[str, tuple]:
    """The SQL condition (on a step `s` and its job's input `i`) for the
    earlier results a character game's job may see and narrate."""
    if job.mode == "creation":
        return " AND i.mode = 'creation' AND s.character_id = ?", (job.character_id,)
    return " AND (i.mode IS NULL OR i.mode != 'creation')", ()


def _validated_reply(reply, job: Job, participants: set[str], characters=None):
    """The reply's (audience, text) messages, asks and resolves, or
    RefereeError(`malformed_reply`). `sender` becomes [the job's sender], and
    is allowed only when this job's input was private (contract section 7).

    Asks are ((participant, character), question) and resolves (participant,
    character) scopes. In a character game (`characters` maps each character
    to its owner and phase), each names an actor: a character ID, or a
    participant ID for that participant's setup question. A setup job may
    address only its sender's setup question, a creation job only its own
    draft and only privately, and a play job any ready character or
    participant, never a draft (character contract sections 5, 7 and 8)."""

    def bad(why):
        return RefereeError(
            "malformed_reply", f"The referee's reply is not usable: {why}."
        )

    def scope_of(actor):
        """The (participant, character) scope an actor names, if this job
        may address it."""
        if not isinstance(actor, str):
            raise bad("a choice names its actor as a string")
        if characters is None:
            if actor not in participants:
                raise bad("a choice names someone who is not a participant")
            return actor, None
        if job.mode == "setup":
            allowed = actor == job.participant_id
        elif job.mode == "creation":
            allowed = actor == job.character_id
        else:
            allowed = actor in participants or (
                is_character_id(actor)
                and characters.get(actor, (None, None))[1] == "ready"
            )
        if not allowed:
            raise bad(f"a {job.mode} reply may not address that actor")
        if actor in participants:
            return actor, None
        return characters[actor][0], actor

    if not isinstance(reply, dict) or set(reply) != {"messages", "choices", "narrated"}:
        raise bad("it needs exactly messages, choices and narrated")
    narrated = reply["narrated"]
    if (
        not isinstance(narrated, list)
        or len(narrated) > MAX_NARRATED
        or not all(
            isinstance(entry, dict)
            and set(entry) == {"job_id", "step_id"}
            and all(isinstance(value, str) for value in entry.values())
            for entry in narrated
        )
    ):
        raise bad(f"narrated lists at most {MAX_NARRATED} job_id/step_id pairs")
    messages = reply["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise bad(f"messages must be a list of 1 to {MAX_MESSAGES}")
    out = []
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"audience", "text"}:
            raise bad("each message has exactly audience and text")
        audience, text = message["audience"], message["text"]
        if audience == "table":
            if job.mode == "creation":  # never redirected: the job fails instead
                raise bad("a creation reply is private to its player")
            target = "table"
        elif audience == "sender":
            if not job.private:
                raise bad("a private message may only answer private input")
            target = [job.participant_id]
        else:
            raise bad("audience is table or sender")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE:
            raise bad(f"message text is 1 to {MAX_MESSAGE} characters")
        out.append((target, text))
    choices = reply["choices"]
    if not isinstance(choices, dict) or set(choices) != {"ask", "resolve"}:
        raise bad("choices has exactly ask and resolve")
    asks, resolves = choices["ask"], choices["resolve"]
    limit = 1 if job.mode == "creation" else MAX_CHOICES
    if (
        not isinstance(asks, list)
        or not isinstance(resolves, list)
        or len(asks) > limit
        or len(resolves) > limit
    ):
        raise bad(f"ask and resolve are lists of at most {limit}")
    key = "participant_id" if characters is None else "actor"
    asked = []
    for ask in asks:
        if not isinstance(ask, dict) or set(ask) != {key, "question"}:
            raise bad(f"each ask has exactly {key} and question")
        question = ask["question"]
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > MAX_QUESTION
        ):
            raise bad(f"a question is 1 to {MAX_QUESTION} characters")
        asked.append((scope_of(ask[key]), question))
    return out, asked, [scope_of(actor) for actor in resolves]
