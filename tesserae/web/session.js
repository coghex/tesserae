// One joined WebSocket session (docs/designs/browser_contract.md section 7),
// or one logged-in session of a lobby host (docs/designs/lobby_contract.md
// sections 3 and 10). Protocol only: no page access. The host answers each
// message with exactly one reply, in order, so replies pair first-in,
// first-out with requests; `revision`, `message`, `referee`, `left`,
// `actor`, and a `role` or `proposal` without a `request_id` are
// unsolicited and never pair. In a campaign with characters (docs/designs/character_contract.md
// section 9) the character messages below are requests like any other.

const UNSOLICITED = new Set(["revision", "message", "referee", "left", "actor", "role", "proposal"]);
// These are replies when they carry a request ID (governance contract section 13).
const PAIRED_WITH_ID = new Set(["role", "proposal"]);

export class SessionClosed extends Error {
  constructor(code, reason, error) {
    super(reason || `closed (${code})`);
    this.code = code; // the WebSocket close code
    this.reason = reason; // the host's close reason, e.g. "session_revoked"
    this.error = error; // the host's last error message before closing, if any
  }

  // A refusal or revocation: the identity must be cleared (close 1008).
  get refused() {
    return this.code === 1008;
  }
}

export function newRequestId() {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return "b-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export class Session {
  // `onEvent(message)` gets unsolicited messages; `onClose(SessionClosed)`
  // is called once, when the socket closes for any reason. After close()
  // neither is called again.
  constructor(url, { onEvent, onClose }) {
    this.onEvent = onEvent;
    this.onClose = onClose;
    this.waiting = []; // {resolve, reject} per sent message, oldest first
    this.lastError = null;
    this.ended = false;
    this.socket = new WebSocket(url);
    this.opened = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("close", reject, { once: true });
    });
    this.opened.catch(() => {}); // a failed open is reported through close
    this.socket.addEventListener("message", (event) => this.receive(event.data));
    this.socket.addEventListener("close", (event) => this.closed(event));
  }

  // Joins with `token`; resolves with the snapshot or rejects with the
  // SessionClosed that follows a refusal. The token is sent once and kept
  // nowhere here.
  async join(token) {
    await this.opened;
    return this.request({ version: 1, type: "join", token });
  }

  // Logs in to a lobby host with a host token; resolves with the `lobby`
  // reply or rejects as join() does. The token is sent once and kept nowhere.
  async login(token) {
    await this.opened;
    return this.request({ version: 1, type: "login", token });
  }

  lobby() {
    return this.request({ version: 1, type: "lobby" });
  }

  // Every select says this page handles characters: a campaign with
  // characters refuses a client that does not.
  select(campaignId) {
    return this.request({ version: 1, type: "select", campaign_id: campaignId, characters: 1 });
  }

  characters() {
    return this.request({ version: 1, type: "characters" });
  }

  // `characterId` null returns to setup questions.
  selectCharacter(characterId) {
    return this.request({ version: 1, type: "character_select", character_id: characterId });
  }

  createCharacter(requestId, name) {
    return this.request({ version: 1, type: "character_create", request_id: requestId, name });
  }

  completeCharacter(requestId, characterId, draftRevision) {
    return this.request({
      version: 1,
      type: "character_complete",
      request_id: requestId,
      character_id: characterId,
      draft_revision: draftRevision,
    });
  }

  archiveCharacter(requestId, characterId) {
    return this.request({
      version: 1,
      type: "character_archive",
      request_id: requestId,
      character_id: characterId,
    });
  }

  leave() {
    return this.request({ version: 1, type: "leave" });
  }

  request(message) {
    if (this.ended || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new SessionClosed(0, "not connected", null));
    }
    const reply = new Promise((resolve, reject) => {
      this.waiting.push({ resolve, reject });
    });
    this.socket.send(JSON.stringify(message));
    return reply;
  }

  // `actor`, in a campaign with characters, is the actor this page believes
  // it has: {character_id, mode}. The host refuses a say whose actor is not
  // the connection's.
  say(requestId, text, isPrivate, actor = null) {
    const message = { version: 1, type: "say", request_id: requestId, text, private: isPrivate };
    if (actor) message.actor = actor;
    return this.request(message);
  }

  sync() {
    return this.request({ version: 1, type: "sync" });
  }

  status() {
    return this.request({ version: 1, type: "referee_status" });
  }

  history(afterSeq = 0) {
    return this.request({ version: 1, type: "messages", after_seq: afterSeq });
  }

  // Campaign governance reads (docs/designs/governance_contract.md
  // sections 7 and 13). Changes are sent with request(), as one exact
  // message the page keeps, so a retry resends it unchanged.
  rulesView(characterId = null) {
    const message = { version: 1, type: "rules_view" };
    if (characterId) message.character_id = characterId;
    return this.request(message);
  }

  rulesList() {
    return this.request({ version: 1, type: "rules_list" });
  }

  rulesExport(revision) {
    return this.request({ version: 1, type: "rules_export", revision });
  }

  requests() {
    return this.request({ version: 1, type: "requests" });
  }

  hostView() {
    return this.request({ version: 1, type: "host_view" });
  }

  hostMessages(afterSeq) {
    return this.request({ version: 1, type: "host_messages", after_seq: afterSeq });
  }

  hostAudit(after) {
    return this.request({ version: 1, type: "host_audit", after });
  }

  hostOperations() {
    return this.request({ version: 1, type: "host_operations" });
  }

  // Ends the session from this side; no further callbacks.
  close() {
    this.ended = true;
    this.fail(new SessionClosed(1000, "disconnected", null));
    this.socket.close(1000);
  }

  receive(data) {
    if (this.ended) return;
    let message;
    try {
      message = JSON.parse(data);
    } catch {
      return; // not from a Tesserae host; nothing to pair it with
    }
    // A campaign host transfer's reply is a `role` with its request ID
    // (docs/designs/governance_contract.md section 13).
    if (UNSOLICITED.has(message.type) && !(PAIRED_WITH_ID.has(message.type) && "request_id" in message)) {
      this.onEvent(message);
      return;
    }
    if (message.type === "error") this.lastError = message;
    const next = this.waiting.shift();
    if (next) next.resolve(message);
  }

  closed(event) {
    if (this.ended) return;
    this.ended = true;
    const closed = new SessionClosed(event.code, event.reason, this.lastError);
    this.fail(closed);
    this.onClose(closed);
  }

  fail(error) {
    for (const next of this.waiting.splice(0)) next.reject(error);
  }
}
