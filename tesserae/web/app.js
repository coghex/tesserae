// The browser page: one application state shared by both screens
// (docs/designs/browser_contract.md sections 4 to 8). Screens only render it;
// changing screens or collapsing the navigation never touches a connection.
// Strings go into the page as text only, never as markup. The token is held
// only while a join is being sent, and never stored or logged.

import { Session, newRequestId } from "/session.js";
import { viewNode } from "/views.js";

const NARROW = "(max-width: 40rem)";
const MAX_TEXT = 2000;

const JOB_STATES = {
  running: "the referee is answering it",
  completed: "answered",
  failed: "the referee could not answer it; say it again",
  interrupted: "interrupted before an answer; say it again",
};

const REFUSALS = {
  authentication_failed:
    "That token was not accepted. Check it, or ask the host for a new one.",
  session_revoked: "Your access to this game was revoked.",
  join_timeout: "The host stopped waiting for the join. Try again.",
};

const state = {
  screen: "console",
  navExpanded: !window.matchMedia(NARROW).matches,
  phase: "out", // "out", "joining" or "joined"
  notice: "",
  me: null, // {gameId, participantId, name, module}
  referee: null, // the host's referee projection for this participant
  view: null, // this participant's view, as the host projected it
  revision: null, // the revision the view was read at
  busyFor: null, // who the referee is answering, when known
  messages: new Map(), // seq to conversation message
  draft: "",
  private: false,
  sending: false,
  composerNote: "",
  noteIsBusy: false, // the note is a busy refusal, cleared once the referee is idle
};

let session = null; // the one live Session, shared by both screens
let statusRead = null; // a referee_status in flight, and whether to read again
let syncRead = null; // a sync in flight, and whether to read again
let renderedView = null; // the view object the state panel shows
const fileTokens = new WeakMap(); // token-entry fieldset to a chosen file's token
let entryGeneration = 0; // bumped on every clear, so an older file read is dropped

const $ = (id) => document.getElementById(id);

// ---- Joining and leaving -------------------------------------------------

async function join(token) {
  if (session) leave();
  clearIdentity();
  state.phase = "joining";
  state.notice = "";
  render();
  const url = `ws://${window.location.host}/`;
  const current = new Session(url, {
    onEvent: (message) => current === session && receive(message),
    onClose: (closed) => current === session && lost(closed),
  });
  session = current;
  let reply;
  try {
    reply = await current.join(token);
  } catch {
    return; // the socket closed; lost() reports why
  }
  if (current !== session) return;
  if (reply.type !== "snapshot") {
    state.notice = refusalText(reply.code, reply.message);
    return; // the host closes next; lost() clears the identity
  }
  applySnapshot(reply);
  state.phase = "joined";
  render();
  $("composer-text").focus();
  try {
    const history = await current.history(0);
    if (current === session && history.type === "messages") {
      merge(history.messages);
      render();
    }
  } catch {
    // The connection closed; lost() has already said so.
  }
}

function leave() {
  const current = session;
  session = null;
  if (current) current.close();
}

function disconnect(notice) {
  leave();
  clearIdentity();
  state.notice = notice;
  render();
}

function lost(closed) {
  session = null;
  const wasJoined = state.phase === "joined";
  clearIdentity();
  if (closed && closed.refused) {
    const code = (closed.error && closed.error.code) || closed.reason;
    const message = closed.error && closed.error.message;
    state.notice = state.notice || refusalText(code, message);
  } else if (wasJoined) {
    state.notice = "The connection to the host was lost. Join again to continue.";
  } else {
    state.notice = state.notice || "Could not reach the host. Is it still running?";
  }
  render();
}

function refusalText(code, message) {
  return REFUSALS[code] || message || "The host refused the connection.";
}

// Clears everything that belongs to the joined participant, so another
// identity never sees it.
function clearIdentity() {
  Object.assign(state, {
    phase: "out",
    me: null,
    referee: null,
    view: null,
    revision: null,
    busyFor: null,
    messages: new Map(),
    draft: "",
    private: false,
    sending: false,
    composerNote: "",
    noteIsBusy: false,
  });
  statusRead = null;
  syncRead = null;
  $("composer-text").value = "";
  $("composer-private").checked = false;
  clearTokenEntries();
}

// Empties every token field and file choice, forgets file tokens, and makes
// any file read still in progress land nowhere.
function clearTokenEntries() {
  entryGeneration += 1;
  for (const entry of document.querySelectorAll(".token-entry")) {
    entry.querySelector('input[type="password"]').value = "";
    entry.querySelector('input[type="file"]').value = "";
    fileTokens.delete(entry);
  }
}

// ---- What the host sends -----------------------------------------------

function applySnapshot(snapshot) {
  state.me = {
    gameId: snapshot.game_id,
    participantId: snapshot.participant_id,
    name: snapshot.participant_name || snapshot.participant_id,
    module: snapshot.module || null,
  };
  state.referee = snapshot.referee || null;
  state.view = snapshot.view ?? null;
  state.revision = snapshot.revision;
}

function receive(message) {
  if (message.type === "message") {
    merge([message]);
  } else if (message.type === "referee") {
    if (state.referee) state.referee.state = message.state;
    if (message.state === "idle") becameIdle();
    readStatus();
  } else if (message.type === "revision") {
    if (state.revision === null || message.revision > state.revision) readView();
  }
  render();
}

// A newer revision means the view may have changed: read a fresh snapshot.
// Revisions coalesce, and reads never overlap; a revision arriving during a
// read is covered by reading again.
async function readView() {
  if (syncRead) {
    syncRead.again = true;
    return;
  }
  const current = session;
  syncRead = { again: false };
  try {
    do {
      syncRead.again = false;
      const reply = await current.sync();
      if (current !== session) return;
      if (reply.type === "snapshot" && reply.revision >= state.revision) {
        applySnapshot(reply);
        if (state.referee && state.referee.state === "idle") becameIdle();
        render();
      }
    } while (syncRead && syncRead.again);
  } catch {
    // The connection closed; lost() has already said so.
  } finally {
    if (current === session) syncRead = null;
  }
}

function becameIdle() {
  state.busyFor = null;
  if (state.noteIsBusy) {
    state.composerNote = "";
    state.noteIsBusy = false;
  }
}

// Conversation messages are kept by seq: duplicates are ignored and arrival
// order never matters. Seqs this player cannot see are simply missing.
function merge(messages) {
  for (const message of messages) {
    if (Number.isInteger(message.seq) && !state.messages.has(message.seq)) {
      state.messages.set(message.seq, message);
    }
  }
}

// A busy/idle change can come with a new question or a finished input and no
// revision, so the projection is read again; reads never overlap.
async function readStatus() {
  if (statusRead) {
    statusRead.again = true;
    return;
  }
  const current = session;
  statusRead = { again: false };
  try {
    do {
      statusRead.again = false;
      const reply = await current.status();
      if (current !== session) return;
      if (reply.type === "referee_status") {
        const { version, type, ...projection } = reply;
        state.referee = projection;
        if (projection.state === "idle") becameIdle();
        render();
      }
    } while (statusRead && statusRead.again);
  } catch {
    // The connection closed; lost() has already said so.
  } finally {
    if (current === session) statusRead = null;
  }
}

// ---- Talking -------------------------------------------------------------

async function send() {
  const current = session;
  if (!current || state.phase !== "joined" || state.sending) return;
  const text = state.draft;
  if (!text.trim()) {
    state.composerNote = "Type what you want to say first.";
    render();
    return;
  }
  if (text.length > MAX_TEXT) {
    state.composerNote = `That is longer than ${MAX_TEXT} characters.`;
    render();
    return;
  }
  state.sending = true;
  state.composerNote = "";
  state.noteIsBusy = false;
  render();
  let reply;
  try {
    reply = await current.say(newRequestId(), text, state.private);
  } catch {
    return; // the connection closed; lost() has already said so
  }
  if (current !== session) return;
  state.sending = false;
  if (reply.type === "accepted") {
    state.draft = "";
    state.private = false;
    $("composer-text").value = "";
    $("composer-private").checked = false;
    state.busyFor = state.me.name;
  } else if (reply.type === "error" && reply.code === "referee_busy") {
    state.busyFor = (reply.job && reply.job.for) || null;
    if (state.referee) state.referee.state = "busy";
    state.composerNote = `${reply.message} Your text is still here.`;
    state.noteIsBusy = true;
  } else {
    state.composerNote = reply.message || "The host could not take that.";
  }
  render();
}

// ---- Rendering -----------------------------------------------------------

function render() {
  renderShell();
  renderConsole();
  renderSettings();
}

function renderShell() {
  $("app").classList.toggle("collapsed", !state.navExpanded);
  $("nav-toggle").setAttribute("aria-expanded", String(state.navExpanded));
  $("nav-toggle-label").textContent = state.navExpanded
    ? "Collapse navigation"
    : "Expand navigation";
  for (const item of document.querySelectorAll("[data-screen]")) {
    if (item.dataset.screen === state.screen) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  for (const name of ["console", "settings"]) {
    $(`screen-${name}`).hidden = name !== state.screen;
  }
}

function statusText() {
  if (state.phase === "joining") return "Joining…";
  if (state.phase !== "joined") return "Not connected";
  let referee = "";
  if (state.referee && state.referee.state === "busy") {
    referee = state.busyFor
      ? ` · The referee is answering ${state.busyFor}.`
      : " · The referee is answering.";
  } else if (state.referee) {
    referee = " · The referee is ready.";
  }
  return `Joined as ${state.me.name}${referee}`;
}

function renderConsole() {
  const joined = state.phase === "joined";
  $("console-status").textContent = statusText();
  showNotice($("console-notice"));
  $("console-join").hidden = joined;
  $("console-play").hidden = !joined;
  for (const button of document.querySelectorAll(".join-button")) {
    button.disabled = state.phase === "joining";
  }
  // Locked while a send awaits its reply, so the reply clears only what was
  // sent, never text typed meanwhile.
  $("composer-send").disabled = !joined || state.sending;
  $("composer-text").readOnly = state.sending;
  $("composer-private").disabled = state.sending;
  $("composer-note").textContent = state.composerNote;
  renderConversation();
  renderQuestion();
  renderLastInput();
  renderView();
}

// Only the viewer's own pending choice is ever in the projection.
function renderQuestion() {
  const [choice] = (state.referee && state.referee.choices) || [];
  $("console-question").hidden = !choice;
  $("console-question-text").textContent = choice ? String(choice.question) : "";
}

function renderLastInput() {
  const last = state.referee && state.referee.your_last_input;
  $("console-last").hidden = !last;
  // Emptied, not just hidden, so no other identity's text stays in the page.
  $("console-last-text").textContent = last
    ? `“${last.text}”${last.private ? " (private)" : ""}`
    : "";
  $("console-last-state").textContent = last
    ? JOB_STATES[last.job_state] || String(last.job_state)
    : "";
}

function renderView() {
  const panel = $("console-state-body");
  if (renderedView === state.view && panel.dataset.revision === String(state.revision)) return;
  renderedView = state.view;
  panel.dataset.revision = String(state.revision);
  panel.replaceChildren(viewNode(state.me && state.me.module, state.view));
  $("console-state-revision").textContent =
    state.revision === null ? "" : `As of revision ${state.revision}.`;
}

function renderConversation() {
  const list = $("conversation");
  const seqs = [...state.messages.keys()].sort((a, b) => a - b);
  if (list.dataset.seqs === seqs.join(",")) return;
  list.dataset.seqs = seqs.join(",");
  const atEnd = list.scrollTop + list.clientHeight >= list.scrollHeight - 8;
  list.replaceChildren(...seqs.map((seq) => messageItem(state.messages.get(seq))));
  if (atEnd) list.scrollTop = list.scrollHeight;
}

function messageItem(message) {
  const item = document.createElement("li");
  item.className = `message ${message.audience === "private" ? "private" : "table"}`;
  item.dataset.seq = String(message.seq);
  const head = document.createElement("p");
  head.className = "message-head";
  const sender = document.createElement("span");
  sender.className = "sender";
  const from = message.from || {};
  sender.textContent = from.kind === "referee" ? "Referee" : String(from.name ?? "?");
  head.append(sender);
  if (message.audience === "private") {
    const badge = document.createElement("span");
    badge.className = "audience";
    badge.textContent = "Private";
    head.append(" ", badge);
  }
  const text = document.createElement("p");
  text.className = "message-text";
  text.textContent = String(message.text ?? "");
  item.append(head, text);
  return item;
}

function renderSettings() {
  const joined = state.phase === "joined";
  showNotice($("settings-notice"));
  $("settings-status").textContent =
    state.phase === "joined" ? "Joined" : state.phase === "joining" ? "Joining…" : "Not connected";
  const me = state.me;
  $("settings-game").textContent = me ? me.gameId : "Shown after joining";
  $("settings-name").textContent = me ? `${me.name} (${me.participantId})` : "Shown after joining";
  $("settings-module").textContent = !me
    ? "Shown after joining"
    : me.module
      ? `${me.module.id} ${me.module.version}`
      : "None";
  $("settings-disconnect").disabled = state.phase === "out";
  $("settings-change").disabled = state.phase === "out";
}

function showNotice(element) {
  element.textContent = state.notice;
  element.hidden = !state.notice;
}

// ---- Wiring --------------------------------------------------------------

function wireTokenEntry(entry) {
  const typed = entry.querySelector('input[type="password"]');
  const file = entry.querySelector('input[type="file"]');
  const tryJoin = () => {
    const token = typed.value.trim() || fileTokens.get(entry) || "";
    if (!token) {
      state.notice = "Paste your token or choose your token file first.";
      render();
      return;
    }
    join(token);
  };
  file.addEventListener("change", async () => {
    fileTokens.delete(entry);
    const chosen = file.files && file.files[0];
    if (!chosen) return;
    const generation = entryGeneration;
    // The token file rule: the token alone, with trailing newlines removed.
    const token = (await chosen.text()).replace(/\n+$/, "");
    if (generation === entryGeneration && file.files[0] === chosen) {
      fileTokens.set(entry, token);
    }
  });
  typed.addEventListener("keydown", (event) => {
    if (event.key === "Enter") tryJoin();
  });
  entry.querySelector(".join-button").addEventListener("click", tryJoin);
}

function start() {
  $("settings-host").textContent = window.location.host;
  $("nav-toggle").addEventListener("click", () => {
    state.navExpanded = !state.navExpanded;
    render();
  });
  for (const item of document.querySelectorAll("[data-screen]")) {
    item.addEventListener("click", () => {
      state.screen = item.dataset.screen;
      render();
    });
  }
  for (const entry of document.querySelectorAll(".token-entry")) wireTokenEntry(entry);

  const text = $("composer-text");
  text.addEventListener("input", () => {
    state.draft = text.value;
  });
  text.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  $("composer-private").addEventListener("change", (event) => {
    state.private = event.target.checked;
  });
  $("composer-send").addEventListener("click", send);

  $("settings-disconnect").addEventListener("click", () => disconnect("Disconnected."));
  $("settings-change").addEventListener("click", () => {
    disconnect("Disconnected. Join with the other token.");
    $("settings-token").focus();
  });
  render();
}

start();
