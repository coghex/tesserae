// One joined WebSocket session (docs/designs/browser_contract.md section 7).
// Protocol only: no page access. The host answers each message with exactly
// one reply, in order, so replies pair first-in, first-out with requests;
// `revision`, `message` and `referee` are unsolicited and never pair.

const UNSOLICITED = new Set(["revision", "message", "referee"]);

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

  say(requestId, text, isPrivate) {
    return this.request({
      version: 1,
      type: "say",
      request_id: requestId,
      text,
      private: isPrivate,
    });
  }

  status() {
    return this.request({ version: 1, type: "referee_status" });
  }

  history(afterSeq = 0) {
    return this.request({ version: 1, type: "messages", after_seq: afterSeq });
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
    if (UNSOLICITED.has(message.type)) {
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
