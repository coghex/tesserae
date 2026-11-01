"""Durable referee state: input, jobs, the busy gate, tool steps, pending
choices, messages, rulings and the Codex session (#65, REF-2).

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
from ..store import GameStore, ParticipantContext, StoreError

REFEREE = "referee"  # the context for referee-only commands; no token maps to it
MAX_TEXT = 2000
MAX_MESSAGE = 4000
MAX_MESSAGES = 8
MAX_CHOICES = 4
MAX_QUESTION = 1000
TRANSCRIPT_PAGE = 200
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
               host_run: str | None = None) -> Submission:  # fmt: skip
        """Accept one player's input, replay an earlier one, or refuse.

        In one transaction: the context's credential must still be active
        (`session_revoked` otherwise); an exact retry (same participant and request ID,
        same text and privacy) returns its recorded job, busy or not; another
        content under that ID is `request_conflict`; otherwise a running job
        means `referee_busy`, with nothing stored; otherwise the input, the
        player's own message and a running job are stored together.
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
                "SELECT text, private FROM referee_inputs WHERE participant_id = ? AND request_id = ?",
                (who, request_id),
            ).fetchone()
            if earlier is not None:
                if (earlier[0], bool(earlier[1])) != (text, private):
                    raise RefereeError(
                        "request_conflict",
                        "That request ID was already used for other input.",
                    )
                job = self._job_for_input(db, who, request_id)
                return Submission("replayed", job, self._reply_messages(db, job))
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
                "INSERT INTO referee_inputs (participant_id, request_id, text, private, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (who, request_id, text, int(private), now),
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
                "INSERT INTO referee_messages (job_id, sender, audience, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, who, json.dumps(audience), text, now),
            )
            return Submission("accepted", self._job(db, job_id), ())

    def running_job(self) -> Job | None:
        return self._running(self.store)

    def job(self, job_id: str) -> Job:
        return self._job(self.store, job_id)

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
            messages, asks, resolves = _validated_reply(reply, job, participants)
            acknowledged = self._acknowledged(db, reply["narrated"], messages)
            now = _now()
            stored = []
            for audience, text in messages:
                seq = db.execute(
                    "INSERT INTO referee_messages (job_id, sender, audience, text, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (job_id, REFEREE, json.dumps(audience), text, now),
                ).lastrowid
                stored.append(_message(seq, REFEREE, audience, text))
            for participant_id in resolves:
                db.execute(
                    "DELETE FROM referee_choices WHERE participant_id = ?",
                    (participant_id,),
                )
            for participant_id, question in asks:
                db.execute(
                    "INSERT INTO referee_choices (participant_id, question, asked_in_job, asked_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (participant_id) DO UPDATE SET "
                    "question = excluded.question, asked_in_job = excluded.asked_in_job, "
                    "asked_at = excluded.asked_at",
                    (participant_id, question, job_id, now),
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
        difference is `request_conflict`, across both kinds of step."""
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
            self._require_running(db, job_id)
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
                    "request_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'intended')",
                    (
                        job_id,
                        step_id,
                        kind,
                        context,
                        command,
                        encoded,
                        f"ref:{job_id}:{step_id}",
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

    def unnarrated_steps(self) -> list[Step]:
        """Committed steps of finished jobs that no published reply has
        narrated yet, oldest first (for `get_context`)."""
        rows = self.store.query(
            "SELECT s.job_id, s.step_id FROM referee_steps AS s JOIN referee_jobs AS j "
            "ON j.job_id = s.job_id WHERE s.status = 'committed' AND s.narrated = 0 "
            "AND j.state != 'running' ORDER BY j.started_at, s.rowid"
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
            "SELECT seq, sender, audience, text FROM referee_messages WHERE seq > ? ORDER BY seq",
            (after_seq,),
        )
        visible = []
        for seq, sender, audience, text in rows:
            audience = json.loads(audience)
            if audience == "table" or participant_id in audience:
                visible.append(_message(seq, sender, audience, text))
        return visible

    def input_message(self, job_id: str) -> dict:
        """The message holding a job's input: the player's own words."""
        [row] = self.store.query(
            "SELECT seq, sender, audience, text FROM referee_messages "
            "WHERE job_id = ? AND sender != ? ORDER BY seq LIMIT 1",
            (job_id, REFEREE),
        )
        return _message(row[0], row[1], json.loads(row[2]), row[3])

    def transcript(self, after_seq: int = 0) -> tuple[list[dict], int | None]:
        """Referee only: the complete conversation, one page at a time, and the
        `after_seq` of the next page (None at the end). Never truncated."""
        rows = self.store.query(
            "SELECT seq, sender, audience, text FROM referee_messages WHERE seq > ? "
            "ORDER BY seq LIMIT ?",
            (after_seq, TRANSCRIPT_PAGE + 1),
        )
        page = [
            _message(*row[:2], json.loads(row[2]), row[3])
            for row in rows[:TRANSCRIPT_PAGE]
        ]
        return page, (page[-1]["seq"] if len(rows) > TRANSCRIPT_PAGE else None)

    def all_choices(self) -> list[dict]:
        """Referee only: every pending choice."""
        return [
            {"participant_id": p, "question": q, "asked_in_job": j}
            for p, q, j in self.store.query(
                "SELECT participant_id, question, asked_in_job FROM referee_choices "
                "ORDER BY asked_at, participant_id"
            )
        ]

    def projection(self, participant_id: str) -> dict:
        """The recipient-scoped referee object for status, join and sync
        (contract section 7): busy or idle, only the viewer's own pending
        choice, the last message seq they can see, and their last input, all
        read from one committed state."""
        with self.store.snapshot():
            return self._projection(participant_id)

    def _projection(self, participant_id: str) -> dict:
        choice = self.store.query(
            "SELECT question FROM referee_choices WHERE participant_id = ?",
            (participant_id,),
        )
        visible = self.messages_for(participant_id)
        last = self.store.query(
            "SELECT i.request_id, i.text, i.private, j.job_id, j.state FROM referee_inputs AS i "
            "JOIN referee_jobs AS j ON j.input_id = i.id WHERE i.participant_id = ? "
            "ORDER BY i.id DESC LIMIT 1",
            (participant_id,),
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
        "j.host_run, j.revision_at_start, j.error, j.narrated FROM referee_jobs AS j "
        "JOIN referee_inputs AS i ON i.id = j.input_id "
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
            "SELECT seq, sender, audience, text FROM referee_messages "
            "WHERE job_id = ? AND sender = ? ORDER BY seq",
            (job.job_id, REFEREE),
        )
        return tuple(
            _message(seq, sender, json.loads(audience), text)
            for seq, sender, audience, text in rows
            if json.loads(audience) == "table"
            or job.participant_id in json.loads(audience)
        )

    def _acknowledged(self, db, narrated, messages) -> list[tuple]:
        """The (job, step) pairs a reply's `narrated` list may acknowledge."""
        pairs = []
        for entry in narrated:
            row = db.execute(
                "SELECT i.participant_id FROM referee_steps AS s "
                "JOIN referee_jobs AS j ON j.job_id = s.job_id "
                "JOIN referee_inputs AS i ON i.id = j.input_id "
                "WHERE s.job_id = ? AND s.step_id = ? AND s.status = 'committed' "
                "AND s.narrated = 0 AND j.state != 'running'",
                (entry["job_id"], entry["step_id"]),
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
            "SELECT job_id, step_id, kind, context, command, args, request_id, status, result "
            "FROM referee_steps WHERE job_id = ? AND step_id = ?",
            (job_id, step_id),
        )[0]
        return Step(
            *row[:5],
            json.loads(row[5]),
            row[6],
            row[7],
            None if row[8] is None else json.loads(row[8]),
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
    )


def _message(seq: int, sender: str, audience, text: str) -> dict:
    return {"seq": seq, "sender": sender, "audience": audience, "text": text}


def _validated_reply(reply, job: Job, participants: set[str]):
    """The reply's (audience, text) messages, asks and resolves, or
    RefereeError(`malformed_reply`). `sender` becomes [the job's sender], and
    is allowed only when this job's input was private (contract section 7)."""

    def bad(why):
        return RefereeError(
            "malformed_reply", f"The referee's reply is not usable: {why}."
        )

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
    if (
        not isinstance(asks, list)
        or not isinstance(resolves, list)
        or len(asks) > MAX_CHOICES
        or len(resolves) > MAX_CHOICES
    ):
        raise bad(f"ask and resolve are lists of at most {MAX_CHOICES}")
    asked = []
    for ask in asks:
        if not isinstance(ask, dict) or set(ask) != {"participant_id", "question"}:
            raise bad("each ask has exactly participant_id and question")
        if ask["participant_id"] not in participants:
            raise bad("a choice names someone who is not a participant")
        question = ask["question"]
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > MAX_QUESTION
        ):
            raise bad(f"a question is 1 to {MAX_QUESTION} characters")
        asked.append((ask["participant_id"], question))
    for participant_id in resolves:
        if participant_id not in participants:
            raise bad("a resolved choice names someone who is not a participant")
    return out, asked, resolves
