// The browser page: one application state shared by both screens
// (docs/designs/browser_contract.md sections 4 to 8). Screens only render it;
// changing screens or collapsing the navigation never touches a connection.
// Strings go into the page as text only, never as markup. The token is held
// in memory only, to reconnect, and never stored or logged.
//
// A lobby host's page (docs/designs/lobby_contract.md section 10) logs in
// with a host token, lists the player's campaigns and enters one. Everything
// of a campaign is cleared when the page leaves it.
//
// In a campaign with characters (docs/designs/character_contract.md section
// 15) the connection also has an actor: no character (setup questions), a
// draft (private creation) or a ready character (play). Everything of one
// actor (its sheet, draft, question, last and unconfirmed inputs) is cleared
// when the actor changes; the campaign's conversation stays.

import { Session, newRequestId } from "/session.js";
import { viewNode } from "/views.js";

const NARROW = "(max-width: 40rem)";
const LOBBY = document.querySelector('meta[name="tesserae-host"]')?.content === "lobby";
const MAX_TEXT = 2000;

const JOB_STATES = {
  running: "the referee is answering it",
  completed: "answered",
  failed: "the referee could not answer it; say it again",
  interrupted: "interrupted before an answer; say it again",
};

const REFRESH_NOTE =
  "Anything you had typed but not sent, or that the referee never accepted, " +
  "is not kept when the page reloads. Your last accepted input is shown above.";
const EDIT_NOTE =
  "Editing makes this a new input. If the referee did receive the first one, " +
  "sending this is a second input.";

const SETTINGS_STATUS = {
  out: "Not connected",
  joining: LOBBY ? "Logging in…" : "Joining…",
  lobby: "Logged in, choosing a campaign",
  selecting: "Opening the campaign…",
  leaving: "Leaving the campaign…",
  joined: LOBBY ? "In a campaign" : "Joined",
  lost: "Disconnected",
  reconnecting: "Reconnecting…",
};

const REFUSALS = {
  authentication_failed: LOBBY
    ? "That token was not accepted. Use your host token, or ask the host for a new one."
    : "That token was not accepted. Check it, or ask the host for a new one.",
  session_revoked: LOBBY
    ? "Your host token is no longer valid. Ask the host for a new one."
    : "Your access to this game was revoked.",
  join_timeout: LOBBY
    ? "The host stopped waiting for the login. Try again."
    : "The host stopped waiting for the join. Try again.",
};

const state = {
  screen: "console",
  navExpanded: !window.matchMedia(NARROW).matches,
  // "out", "joining", "joined", "lost" or "reconnecting"; a lobby host's page
  // also has "lobby" (logged in), "selecting" and "leaving".
  phase: "out",
  notice: "",
  player: null, // a lobby host's logged-in player: {id, name}
  catalog: [], // the campaigns open to that player, as last listed
  campaign: null, // the selected campaign: {campaignId, name}
  me: null, // {gameId, participantId, name, module}
  referee: null, // the host's referee projection for this participant
  view: null, // this participant's view, as the host projected it
  revision: null, // the revision the view was read at
  busyFor: null, // who the referee is answering, when known
  messages: new Map(), // seq to conversation message
  draft: "",
  private: false,
  sending: false, // the input awaiting its reply: {requestId, text, private, ...}
  unconfirmed: null, // sent, then the connection dropped before any reply
  lastAccepted: null, // this page's last input the host accepted
  composerNote: "",
  noteIsBusy: false, // the note is a busy refusal, cleared once the referee is idle
  // A campaign with characters: the host's word on this connection's actor
  // ({mode, character}), null in a campaign without characters; the
  // player's characters as last listed; the sheet and the draft revision it
  // was shown at (what Approve completes); and the character panels' note.
  actor: null,
  characters: [],
  sheet: null,
  sheetRevision: null,
  choosing: false, // a character request awaits its reply
  confirmingArchive: false,
  characterNote: "",
};

let session = null; // the one live Session, shared by both screens
let heldToken = null; // in memory only, to reconnect the joined identity
let scope = {}; // replaced whenever the game, campaign or actor state is cleared
let campaignScope = {}; // replaced only when the game or campaign state is cleared
const seenGames = new Set(); // games joined since this page loaded
let statusRead = null; // a referee_status in flight, and whether to read again
let syncRead = null; // a sync in flight, and whether to read again
let renderedCatalog = null; // the catalog the picker shows
let renderedView = null; // the view object the state panel shows
let renderedCharacters = null; // the character list the picker shows
let renderedSheet = null; // the sheet object the sheet panel shows
const fileTokens = new WeakMap(); // token-entry fieldset to a chosen file's token
let entryGeneration = 0; // bumped on every clear, so an older file read is dropped

const $ = (id) => document.getElementById(id);

// ---- Joining and leaving -------------------------------------------------

async function join(token) {
  if (session) closeSession();
  clearIdentity();
  heldToken = token;
  state.phase = "joining";
  state.notice = "";
  render();
  await connect(false);
}

// Joins the same identity again after a dropped connection. What is shown,
// the draft and any unconfirmed input stay until the host answers.
async function reconnect() {
  if (state.phase !== "lost" || heldToken === null) return;
  state.phase = "reconnecting";
  state.notice = "";
  render();
  await connect(true);
}

async function connect(again) {
  const url = `ws://${window.location.host}/`;
  const current = new Session(url, {
    onEvent: (message) => current === session && receive(message),
    onClose: (closed) => current === session && lost(closed),
  });
  session = current;
  let reply;
  try {
    reply = LOBBY ? await current.login(heldToken) : await current.join(heldToken);
  } catch {
    return; // the socket closed; lost() reports why
  }
  if (current !== session) return;
  if (reply.type !== (LOBBY ? "lobby" : "snapshot")) {
    state.notice = refusalText(reply.code, reply.message);
    return; // the host closes next; lost() clears the identity
  }
  if (LOBBY) await loggedIn(current, reply, again);
  else await entered(current, reply, again);
}

// After a game's join or a campaign's select. A reconnect that finds another
// participant clears the old one's state first.
async function entered(current, snapshot, again) {
  if (again && state.me && snapshot.participant_id !== state.me.participantId) {
    clearGame(); // never another identity's data
  }
  // A reconnect returns to the same character too; selecting plays nothing.
  // Refused (archived, relinked), the page is back at the picker with why.
  const wanted = again && state.actor && state.actor.character;
  if (wanted && snapshot.actor) {
    const campaignHere = campaignScope;
    let reply;
    try {
      reply = await current.selectCharacter(wanted.character_id);
    } catch {
      return; // the connection closed; lost() has already said so
    }
    if (!stillIn(current, campaignHere) || leftCampaign(reply)) return;
    if (reply.type === "snapshot") {
      snapshot = reply;
    } else {
      // Back in setup now, so applying the setup snapshot below changes
      // nothing more and the explanation stays.
      changeActor(snapshot.actor);
      state.characterNote = `${characterLabel(wanted)} cannot be played now: ${reply.message}`;
    }
  }
  applySnapshot(snapshot);
  const here = scope; // after any change of actor the snapshot made
  state.phase = "joined";
  settleUnconfirmed();
  if (!seenGames.has(state.me.gameId) && state.referee && state.referee.your_last_input) {
    state.composerNote = REFRESH_NOTE;
  }
  seenGames.add(state.me.gameId);
  render();
  if (state.actor) listCharacters();
  if (!again) $(state.referee ? "composer-text" : "console-leave").focus();
  if (!state.referee) return; // a campaign without a referee has no conversation
  try {
    // The whole visible history, on every join: merged by seq, so whatever
    // arrived live or out of order is never duplicated or lost.
    const history = await current.history(0);
    if (!live(current, here) || leftCampaign(history)) return;
    if (history.type === "messages") {
      merge(history.messages);
      render();
    }
  } catch {
    // The connection closed; lost() has already said so.
  }
}

function closeSession() {
  const current = session;
  session = null;
  if (current) current.close();
}

function disconnect(notice) {
  closeSession();
  clearIdentity();
  state.notice = notice;
  render();
}

// True while `current` is the live session and nothing it was asked for has
// been cleared since: a reply for a game or campaign the page has left, or
// from a closed connection, is dropped.
function live(current, here) {
  return current === session && here === scope;
}

// True while `current` is the live session and the page is still in the
// campaign (and as the participant) it was when it asked: a character reply
// may change the actor, but never after the page has left that campaign.
function stillIn(current, campaignHere) {
  return current === session && campaignHere === campaignScope;
}

// ---- A lobby host's campaigns ---------------------------------------------

// After the login reply. A reconnect from inside a campaign enters that
// campaign again; selecting plays nothing, and the host decides whether the
// player still has it.
async function loggedIn(current, lobby, again) {
  applyLobby(lobby);
  const wanted = again ? state.campaign : null;
  if (!wanted) {
    state.phase = "lobby";
    render();
    if (!again) focusPicker();
    return;
  }
  await select(wanted.campaignId, true);
}

function applyLobby(lobby) {
  state.player = { id: lobby.player_id, name: lobby.player_name };
  state.catalog = Array.isArray(lobby.campaigns) ? lobby.campaigns : [];
}

// Enters a campaign from the picker, or again after a reconnect (`again`),
// when what was shown stays until the host answers.
async function select(campaignId, again = false) {
  const current = session;
  if (!current || state.phase !== (again ? "reconnecting" : "lobby")) return;
  if (!again) state.phase = "selecting";
  state.notice = "";
  render();
  let reply;
  try {
    reply = await current.select(campaignId);
  } catch {
    return; // the connection closed; lost() has already said so
  }
  if (current !== session) return;
  if (reply.type === "snapshot" && reply.campaign) {
    await entered(current, reply, again);
    return;
  }
  backInLobby(reply.message || "That campaign cannot be opened right now.");
}

// Returns to the picker. Everything of the campaign is cleared at once, so
// nothing it still sends before the host's reply can appear.
async function leaveCampaign() {
  const current = session;
  if (!current || state.phase !== "joined" || !LOBBY) return;
  clearGame();
  state.phase = "leaving";
  render();
  let reply;
  try {
    reply = await current.leave();
  } catch {
    return; // the connection closed; lost() has already said so
  }
  if (current !== session || state.phase !== "leaving") return;
  state.phase = "lobby";
  // Access may have ended first (no_campaign): the page is in the lobby anyway.
  if (reply.type === "lobby") applyLobby(reply);
  render();
  if (reply.type !== "lobby") listCampaigns();
  focusPicker();
}

// The host ended the campaign for this connection (a `left`, or a request
// answered `campaign_unavailable`): back to the picker, with the reason.
function backInLobby(notice) {
  clearGame();
  state.phase = "lobby";
  state.notice = notice;
  render();
  listCampaigns();
}

// A reply that says the campaign is gone takes the page back to the picker.
function leftCampaign(reply) {
  if (!LOBBY || reply.type !== "error" || reply.code !== "campaign_unavailable") {
    return false;
  }
  backInLobby(reply.message || "That campaign is no longer available to you.");
  return true;
}

async function listCampaigns() {
  const current = session;
  if (!current || state.phase !== "lobby") return;
  let reply;
  try {
    reply = await current.lobby();
  } catch {
    return; // the connection closed; lost() has already said so
  }
  if (current === session && reply.type === "lobby") {
    applyLobby(reply);
    render();
  }
}

function focusPicker() {
  const first = $("campaign-list").querySelector("button");
  (first || $("lobby-refresh")).focus();
}

// A dropped connection keeps everything shown, marked out of date, with the
// token and draft, and offers Reconnect; an input sent without an answer
// becomes unconfirmed. A refusal or revocation (close 1008) clears it all.
function lost(closed) {
  session = null;
  const refused = closed && closed.refused;
  const connected = LOBBY
    ? state.player !== null &&
      ["lobby", "selecting", "leaving", "joined", "reconnecting"].includes(state.phase)
    : state.me !== null && ["joined", "reconnecting"].includes(state.phase);
  if (!refused && connected) {
    const reconnecting = state.phase === "reconnecting";
    if (state.sending && state.sending.fromComposer) {
      state.unconfirmed = { ...state.sending, fresh: false };
    }
    state.sending = false;
    state.choosing = false;
    state.confirmingArchive = false;
    statusRead = null;
    syncRead = null;
    state.phase = "lost";
    state.notice = reconnecting ? "Could not reach the host. Try again once it is running." : "";
    render();
    return;
  }
  clearIdentity();
  if (refused) {
    const code = (closed.error && closed.error.code) || closed.reason;
    const message = closed.error && closed.error.message;
    state.notice = state.notice || refusalText(code, message);
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
  clearGame();
  Object.assign(state, { phase: "out", player: null, catalog: [] });
  heldToken = null;
  clearTokenEntries();
}

// Clears everything of the game or campaign joined: its participant, view,
// conversation, inputs and draft. Replies to anything asked before are then
// dropped (live()).
function clearGame() {
  scope = {};
  campaignScope = {};
  Object.assign(state, {
    campaign: null,
    actor: null,
    characters: [],
    sheet: null,
    sheetRevision: null,
    choosing: false,
    confirmingArchive: false,
    characterNote: "",
    me: null,
    referee: null,
    view: null,
    revision: null,
    busyFor: null,
    messages: new Map(),
    draft: "",
    private: false,
    sending: false,
    unconfirmed: null,
    lastAccepted: null,
    composerNote: "",
    noteIsBusy: false,
  });
  statusRead = null;
  syncRead = null;
  $("composer-text").value = "";
  $("composer-private").checked = false;
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

// Everything of one actor: cleared whenever the actor changes, so nothing of
// one character (or draft) is ever shown, sent or resent as another. The
// conversation is the campaign's and stays. Replies to anything asked
// before are dropped (live()).
function clearActor() {
  scope = {};
  Object.assign(state, {
    sheet: null,
    sheetRevision: null,
    draft: "",
    private: false,
    sending: false,
    unconfirmed: null,
    lastAccepted: null,
    composerNote: "",
    noteIsBusy: false,
    confirmingArchive: false,
    characterNote: "",
  });
  if (state.referee) state.referee = { ...state.referee, choices: [], your_last_input: null };
  statusRead = null;
  syncRead = null;
  $("composer-text").value = "";
  $("composer-private").checked = false;
}

function actorKey(actor) {
  if (!actor) return "";
  return `${actor.character ? actor.character.character_id : ""}/${actor.mode}`;
}

// Follows the host's word on the actor; a different one clears the old one's
// state first. Returns whether it changed.
function changeActor(actor) {
  const changed = actorKey(actor) !== actorKey(state.actor);
  if (changed && state.actor) clearActor();
  state.actor = actor;
  return changed;
}

function applySnapshot(snapshot) {
  if (snapshot.actor) {
    changeActor(snapshot.actor);
    state.sheet = snapshot.sheet ?? null;
    // What Approve completes: the revision this sheet was read at.
    const character = snapshot.actor.character;
    state.sheetRevision = snapshot.actor.mode === "creation" ? character.draft_revision : null;
  }
  state.me = {
    gameId: snapshot.game_id,
    participantId: snapshot.participant_id,
    name: snapshot.participant_name || snapshot.participant_id,
    module: snapshot.module || null,
  };
  if (snapshot.campaign) {
    state.campaign = { campaignId: snapshot.campaign.campaign_id, name: snapshot.campaign.name };
  }
  state.referee = snapshot.referee || null;
  state.view = snapshot.view ?? null;
  state.revision = snapshot.revision;
}

function receive(message) {
  if (message.type === "left") {
    if (state.phase === "joined" && state.campaign) {
      backInLobby(`You no longer have access to ${state.campaign.name}.`);
    }
    return;
  }
  // Inside a lobby host, only a campaign the page is in can speak to it.
  if (LOBBY && state.phase !== "joined") return;
  if (message.type === "actor") {
    // Another connection completed or archived this page's character.
    if (!state.actor) return;
    changeActor(message.actor);
    state.characterNote =
      message.reason === "archived"
        ? "Your character was archived from another page or client."
        : "Your character was completed from another page or client: it is ready to play.";
    readView(); // its sheet and status, as the host now has them
    if (message.actor.mode === "setup") listCharacters();
  } else if (message.type === "message") {
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
  const here = scope;
  const mine = (syncRead = { again: false });
  try {
    do {
      mine.again = false;
      const reply = await current.sync();
      if (!live(current, here) || leftCampaign(reply)) return;
      if (reply.type === "snapshot" && reply.revision >= state.revision) {
        applySnapshot(reply);
        if (state.referee && state.referee.state === "idle") becameIdle();
        render();
      }
    } while (syncRead === mine && mine.again);
  } catch {
    // The connection closed; lost() has already said so.
  } finally {
    if (syncRead === mine) syncRead = null;
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
  const here = scope;
  const mine = (statusRead = { again: false });
  try {
    do {
      mine.again = false;
      const reply = await current.status();
      if (!live(current, here) || leftCampaign(reply)) return;
      if (reply.type === "referee_status") {
        const { version, type, ...projection } = reply;
        state.referee = projection;
        if (projection.state === "idle") becameIdle();
        render();
      }
    } while (statusRead === mine && mine.again);
  } catch {
    // The connection closed; lost() has already said so.
  } finally {
    if (statusRead === mine) statusRead = null;
  }
}

// ---- Characters (a campaign with characters) --------------------------------

function characterLabel(character) {
  if (!character) return "this character";
  if (character.phase === "draft") return character.name ? `${character.name} (draft)` : "an unnamed draft";
  return character.name || "an unnamed character";
}

// The player's own characters, for the picker. A selection the host no
// longer has for this page (archived, relinked) is back in setup here.
async function listCharacters() {
  const current = session;
  const here = scope;
  const campaignHere = campaignScope;
  if (!current || state.phase !== "joined" || !state.actor) return;
  let reply;
  try {
    reply = await current.characters();
  } catch {
    return; // the connection closed; lost() has already said so
  }
  if (!stillIn(current, campaignHere) || leftCampaign(reply)) return;
  if (reply.type !== "characters") return;
  state.characters = reply.characters;
  if (here === scope && changeActor(reply.actor)) readView();
  render();
}

// Plays a ready character, resumes a draft, or (null) asks setup questions
// only. Changes this page only: nothing is drawn, spent or started.
async function chooseCharacter(characterId) {
  const current = session;
  const campaignHere = campaignScope;
  if (!current || state.phase !== "joined" || !state.actor || state.choosing) return;
  state.choosing = true;
  state.characterNote = "";
  render();
  let reply;
  try {
    reply = await current.selectCharacter(characterId);
  } catch {
    return; // the connection closed; lost() has already said so
  } finally {
    if (stillIn(current, campaignHere)) state.choosing = false;
  }
  if (!stillIn(current, campaignHere) || leftCampaign(reply)) return;
  if (reply.type === "snapshot") {
    applySnapshot(reply);
    render();
    if (state.referee) $("composer-text").focus();
  } else {
    state.characterNote = reply.message || "That character cannot be chosen now.";
    render();
  }
  if (state.actor && state.actor.mode === "setup") listCharacters();
}

// Starts a draft, then resumes it: creation is with the referee, privately.
async function createCharacter() {
  const current = session;
  const campaignHere = campaignScope;
  if (!current || state.phase !== "joined" || !state.actor || state.choosing) return;
  const name = $("create-name").value.trim() || null;
  state.choosing = true;
  state.characterNote = "";
  render();
  let reply;
  try {
    reply = await current.createCharacter(newRequestId(), name);
  } catch {
    return;
  } finally {
    if (stillIn(current, campaignHere)) state.choosing = false;
  }
  if (!stillIn(current, campaignHere) || leftCampaign(reply)) return;
  if (reply.type !== "character") {
    state.characterNote = reply.message || "The host could not start a character.";
    render();
    return;
  }
  $("create-name").value = "";
  await chooseCharacter(reply.character.character_id);
}

// Completes the draft at the revision of the sheet shown, never a newer one.
async function approve() {
  const current = session;
  const here = scope;
  const campaignHere = campaignScope;
  const actor = state.actor;
  if (!current || state.phase !== "joined" || !actor || actor.mode !== "creation") return;
  if (state.choosing || state.sheetRevision === null) return;
  const draft = actor.character;
  state.choosing = true;
  render();
  let reply;
  try {
    reply = await current.completeCharacter(newRequestId(), draft.character_id, state.sheetRevision);
  } catch {
    return;
  } finally {
    if (stillIn(current, campaignHere)) state.choosing = false;
  }
  if (!live(current, here) || leftCampaign(reply)) return;
  if (reply.type === "character") {
    changeActor(reply.actor);
    state.characterNote =
      `${characterLabel(reply.character)} is ready. The referee brings them in ` +
      "when you next say something.";
    readView();
  } else if (reply.code === "stale_draft") {
    state.characterNote = "The draft changed since you saw it; review it again.";
    readView();
  } else if (reply.code === "character_incomplete") {
    state.characterNote = `Not finished yet: ${(reply.problems || []).join(" ")}`;
  } else {
    state.characterNote = reply.message || "The host could not complete it.";
  }
  render();
}

// Settings: back to the picker, with no character.
function changeCharacter() {
  state.screen = "console";
  chooseCharacter(null);
}

function askArchive() {
  if (!state.actor || !state.actor.character || state.phase !== "joined") return;
  state.confirmingArchive = true;
  render();
  $("archive-no").focus();
}

async function archive() {
  const current = session;
  const campaignHere = campaignScope;
  const actor = state.actor;
  if (!current || !state.confirmingArchive || !actor || !actor.character) return;
  const character = actor.character;
  state.confirmingArchive = false;
  state.choosing = true;
  render();
  let reply;
  try {
    reply = await current.archiveCharacter(newRequestId(), character.character_id);
  } catch {
    return;
  } finally {
    if (stillIn(current, campaignHere)) state.choosing = false;
  }
  if (!stillIn(current, campaignHere) || leftCampaign(reply)) return;
  if (reply.type === "character") {
    changeActor(reply.actor);
    state.characterNote = `${characterLabel(reply.character)} is archived; its history stays.`;
    readView();
    listCharacters();
  } else {
    state.characterNote = reply.message || "The host could not archive it.";
  }
  render();
}

// ---- Talking -------------------------------------------------------------

async function send() {
  if (!session || state.phase !== "joined" || state.sending || state.unconfirmed) return;
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
  const input = { requestId: newRequestId(), text, private: state.private, actor: sayActor() };
  await submit({ ...input, fromComposer: true, fresh: true });
}

// Sends one input and settles it by the host's reply. `fromComposer` means
// the composer holds its text; `fresh` means it was never sent before.
async function submit(input) {
  const current = session;
  const here = scope;
  state.sending = input;
  state.composerNote = "";
  state.noteIsBusy = false;
  render();
  let reply;
  try {
    reply = await current.say(input.requestId, input.text, input.private, input.actor);
  } catch {
    return; // the connection closed; lost() has already said so
  }
  if (!live(current, here) || leftCampaign(reply)) return;
  state.sending = false;
  const wasUnconfirmed = state.unconfirmed && state.unconfirmed.requestId === input.requestId;
  if (reply.type === "accepted") {
    if (wasUnconfirmed) state.unconfirmed = null;
    acceptedInput(input);
    if (input.fresh) state.busyFor = state.me.name;
  } else if (reply.type === "error" && reply.code === "referee_busy") {
    state.busyFor = (reply.job && reply.job.for) || null;
    if (state.referee) state.referee.state = "busy";
    // An exact resend of an accepted input is answered `accepted`, busy or
    // not, so a busy refusal proves this one was never accepted.
    if (wasUnconfirmed) state.unconfirmed = null;
    const where = input.fromComposer ? " Your text is still here." : "";
    state.composerNote = `${reply.message}${where}`;
    state.noteIsBusy = true;
  } else if (
    reply.type === "error" &&
    ["character_changed", "character_unavailable"].includes(reply.code)
  ) {
    // This page's idea of its character was out of date: nothing was stored.
    // Read where the host has it now; a changed actor clears the old one's.
    state.composerNote = reply.message;
    readView();
    listCharacters();
  } else {
    state.composerNote = reply.message || "The host could not take that.";
  }
  render();
}

// The actor a say names: the one this page believes it has.
function sayActor() {
  if (!state.actor) return null;
  const character = state.actor.character;
  return { character_id: character ? character.character_id : null, mode: state.actor.mode };
}

function acceptedInput(input) {
  state.lastAccepted = {
    requestId: input.requestId,
    text: input.text,
    private: input.private,
    actor: input.actor,
  };
  if (input.fromComposer) {
    state.draft = "";
    state.private = false;
    $("composer-text").value = "";
    $("composer-private").checked = false;
  }
}

// After a reconnect: an unconfirmed input the host reports as the latest
// accepted was accepted. Any other stays unconfirmed: a newer input from
// another page or the CLI can be the latest, so it may still have been.
function settleUnconfirmed() {
  const pending = state.unconfirmed;
  const last = state.referee && state.referee.your_last_input;
  if (pending && last && last.request_id === pending.requestId) {
    state.unconfirmed = null;
    acceptedInput(pending);
  }
}

// What Resend exactly sends from the last-input panel: this page's last
// accepted input, else the one the host reports (the CLI's /retry rule).
function resendTarget() {
  if (state.lastAccepted) return state.lastAccepted;
  const last = state.referee && state.referee.your_last_input;
  return last
    ? { requestId: last.request_id, text: last.text, private: last.private, actor: sayActor() }
    : null;
}

function resend(input, fromComposer) {
  if (!session || state.phase !== "joined" || state.sending || !input) return;
  submit({ ...input, fromComposer, fresh: false });
}

function editUnconfirmed() {
  state.unconfirmed = null;
  state.composerNote = EDIT_NOTE;
  render();
  $("composer-text").focus();
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
  if (state.phase === "joining") return LOBBY ? "Logging in…" : "Joining…";
  if (state.phase === "reconnecting") return "Reconnecting…";
  if (state.phase === "lost") return "Disconnected. What you see may be out of date.";
  if (state.phase === "lobby") return `Logged in as ${state.player.name}`;
  if (state.phase === "selecting") return "Opening the campaign…";
  if (state.phase === "leaving") return "Leaving the campaign…";
  if (state.phase !== "joined") return "Not connected";
  let referee = "";
  if (state.referee && state.referee.state === "busy") {
    referee = state.busyFor
      ? ` · The referee is answering ${state.busyFor}.`
      : " · The referee is answering.";
  } else if (state.referee) {
    referee = " · The referee is ready.";
  }
  const where = state.campaign ? `${state.campaign.name} · ` : "";
  return `${where}${actorText()}Joined as ${state.me.name}${referee}`;
}

// Who this connection acts as, for the status line.
function actorText() {
  const actor = state.actor;
  if (!actor) return "";
  if (actor.mode === "play") return `${actor.character.name} · `;
  if (actor.mode === "creation") {
    return `Creating ${characterLabel(actor.character)} · private to you · `;
  }
  return "No character · ";
}

// Inside a game or campaign, connected or showing it while disconnected.
function inGame() {
  return state.me !== null && ["joined", "lost", "reconnecting"].includes(state.phase);
}

function renderConsole() {
  const joined = state.phase === "joined";
  const shown = inGame();
  const picking = LOBBY && !shown && state.player !== null;
  $("console-status").textContent = statusText();
  $("console-reconnect").hidden = state.phase !== "lost";
  $("console-leave").hidden = !LOBBY || !joined;
  showNotice($("console-notice"));
  $("console-join").hidden = shown || picking;
  $("console-lobby").hidden = !picking;
  $("console-play").hidden = !shown;
  $("console-play").classList.toggle("stale", shown && !joined);
  for (const button of document.querySelectorAll(".join-button")) {
    button.disabled = state.phase === "joining" || state.phase === "reconnecting";
  }
  renderPicker();
  renderCharacters(shown);
  renderSheet(shown);
  const talking = !shown || state.referee !== null;
  $("console-no-referee").hidden = talking;
  $("console-composer").hidden = !talking;
  const mode = state.actor && state.actor.mode;
  $("composer-label").textContent =
    mode === "setup"
      ? "Ask a setup or rules question (nothing in the game changes)"
      : mode === "creation"
        ? `Say to the referee, privately, while creating ${characterLabel(state.actor.character)}`
        : "Say to the referee";
  // Creation is always private: there is nothing to choose.
  $("composer-private-label").hidden = mode === "creation";
  // Locked while a send awaits its reply, so the reply clears only what was
  // sent, never text typed meanwhile, and while an unconfirmed input is
  // being settled.
  const locked = Boolean(state.sending || state.unconfirmed);
  $("composer-send").disabled = !joined || locked;
  $("composer-text").readOnly = locked;
  $("composer-private").disabled = locked;
  $("composer-note").textContent = state.composerNote;
  renderConversation();
  renderQuestion();
  renderUnconfirmed();
  renderLastInput();
  renderView();
}

// The player's campaigns, each a button that enters it.
function renderPicker() {
  const player = state.player;
  $("lobby-player").textContent = player ? `Logged in as ${player.name}.` : "";
  $("lobby-empty").hidden = state.catalog.length > 0;
  const list = $("campaign-list");
  if (renderedCatalog !== state.catalog) {
    renderedCatalog = state.catalog;
    list.replaceChildren(
      ...state.catalog.map((campaign) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "campaign";
        button.textContent = String(campaign.name);
        button.dataset.campaignId = String(campaign.campaign_id);
        item.append(button);
        return item;
      }),
    );
  }
  const usable = state.phase === "lobby";
  for (const button of list.querySelectorAll("button")) button.disabled = !usable;
  $("lobby-refresh").disabled = !usable;
}

// In setup: Play for each ready character, Resume for each draft, Create.
function renderCharacters(shown) {
  const picking = shown && state.actor !== null && state.actor.mode === "setup";
  $("console-characters").hidden = !picking;
  $("characters-empty").hidden = state.characters.length > 0;
  const list = $("character-list");
  if (renderedCharacters !== state.characters) {
    renderedCharacters = state.characters;
    list.replaceChildren(
      ...state.characters.map((character) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "campaign";
        button.dataset.characterId = String(character.character_id);
        const draft = character.phase === "draft";
        button.append(draft ? "Resume " : "Play ", characterLabel(character));
        if (draft) {
          const kind = document.createElement("span");
          kind.className = "kind";
          kind.textContent = `, revision ${character.draft_revision}`;
          button.append(kind);
        }
        item.append(button);
        return item;
      }),
    );
  }
  const usable = state.phase === "joined" && !state.choosing;
  for (const button of list.querySelectorAll("button")) button.disabled = !usable;
  $("character-create").disabled = !usable;
}

// The selected character's sheet, read-only: the module's data, as shown.
function renderSheet(shown) {
  const actor = state.actor;
  const visible = shown && actor !== null && actor.mode !== "setup";
  $("console-sheet").hidden = !visible && !state.characterNote;
  $("sheet-note").textContent = state.characterNote;
  $("sheet-body").hidden = !visible;
  $("sheet-revision").hidden = !visible;
  const creating = visible && actor.mode === "creation";
  $("sheet-title").textContent = !visible
    ? "Your character"
    : creating
      ? `Creating ${characterLabel(actor.character)}`
      : characterLabel(actor.character);
  $("sheet-revision").textContent = creating && state.sheetRevision !== null
    ? `Draft revision ${state.sheetRevision}. Only you see this.`
    : "";
  const approveButton = $("character-approve");
  approveButton.hidden = !creating || state.sheetRevision === null;
  approveButton.textContent = creating
    ? `Approve ${characterLabel(actor.character)} (revision ${state.sheetRevision})`
    : "Approve";
  approveButton.disabled = state.phase !== "joined" || state.choosing;
  if (renderedSheet !== state.sheet) {
    renderedSheet = state.sheet;
    $("sheet-body").replaceChildren(viewNode(null, state.sheet));
  }
}

function renderUnconfirmed() {
  const pending = state.unconfirmed;
  $("console-unconfirmed").hidden = !pending;
  $("console-unconfirmed-text").textContent = pending
    ? `“${pending.text}”${pending.private ? " (private)" : ""}`
    : "";
  const usable = state.phase === "joined" && !state.sending;
  $("unconfirmed-resend").disabled = !usable;
  $("unconfirmed-edit").disabled = !usable;
}

// Only the viewer's own pending choice is ever in the projection.
function renderQuestion() {
  const [choice] = (state.referee && state.referee.choices) || [];
  $("console-question").hidden = !choice;
  $("console-question-text").textContent = choice ? String(choice.question) : "";
}

function renderLastInput() {
  const last = state.referee && state.referee.your_last_input;
  // Emptied, not just hidden, so no other identity's text stays in the page.
  $("console-last-text").textContent = last
    ? `“${last.text}”${last.private ? " (private)" : ""}`
    : "";
  $("console-last-state").textContent = last
    ? JOB_STATES[last.job_state] || String(last.job_state)
    : "";
  const target = resendTarget();
  $("console-resend").hidden = !target || Boolean(state.unconfirmed);
  $("console-resend").disabled = state.phase !== "joined" || Boolean(state.sending);
  // Say what Resend sends when it is not the input shown above.
  const other = target && (!last || last.request_id !== target.requestId);
  $("console-resend-target").textContent = other ? `Resend sends “${target.text}”.` : "";
  $("console-last").hidden = !last && !target;
}

function renderView() {
  const panel = $("console-state-body");
  const stale = state.phase !== "joined" ? " It may be out of date." : "";
  $("console-state-revision").textContent =
    state.revision === null ? "" : `As of revision ${state.revision}.${stale}`;
  if (renderedView === state.view && panel.dataset.revision === String(state.revision)) return;
  renderedView = state.view;
  panel.dataset.revision = String(state.revision);
  panel.replaceChildren(viewNode(state.me && state.me.module, state.view));
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
  if (from.kind !== "referee" && typeof from.character === "string") {
    sender.textContent += ` as ${from.character}`;
  }
  head.append(sender);
  if (message.audience === "private") {
    const badge = document.createElement("span");
    badge.className = "audience";
    const creation = message.creation;
    if (creation && typeof creation === "object") {
      badge.classList.add("creation");
      badge.textContent = `Private · creating ${creation.name ? String(creation.name) : "a draft"}`;
    } else {
      badge.textContent = "Private";
    }
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
  $("settings-status").textContent = SETTINGS_STATUS[state.phase];
  const { player, campaign } = state;
  $("settings-player").textContent = player
    ? `${player.name} (${player.id})`
    : "Shown after logging in";
  $("settings-campaign").textContent = campaign
    ? `${campaign.name} (${campaign.campaignId})`
    : player
      ? "None: choose one in the Console"
      : "Shown after choosing one";
  $("settings-leave").disabled = !joined;
  const actor = state.actor;
  const character = actor && actor.character;
  $("settings-character").textContent = !actor
    ? campaign
      ? "This campaign has no characters"
      : "Shown after choosing one"
    : character
      ? `${characterLabel(character)} (${character.character_id}, ${
          actor.mode === "creation" ? "being created" : "ready"
        })`
      : "None: questions only";
  $("settings-change-character").disabled = !joined || !character || state.choosing;
  $("settings-archive").disabled = !joined || !character || state.choosing;
  $("archive-confirm").hidden = !state.confirmingArchive || !character;
  $("archive-confirm-text").textContent = character
    ? `Archive ${characterLabel(character)}? It leaves your list for good; its ` +
      "history stays in the campaign."
    : "";
  $("archive-yes").textContent = character ? `Archive ${characterLabel(character)}` : "Archive";
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
  if (LOBBY) {
    for (const node of document.querySelectorAll(".lobby-only")) node.hidden = false;
    for (const node of document.querySelectorAll(".game-only")) node.hidden = true;
    for (const button of document.querySelectorAll(".join-button")) {
      button.textContent = "Log in";
    }
    $("console-join-title").textContent = "Log in";
    $("console-join-text").textContent =
      "Paste your host token or choose your token file, then log in.";
  }
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

  $("console-reconnect").addEventListener("click", reconnect);
  for (const button of document.querySelectorAll(".leave-button")) {
    button.addEventListener("click", leaveCampaign);
  }
  $("campaign-list").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-campaign-id]");
    if (button) select(button.dataset.campaignId);
  });
  $("lobby-refresh").addEventListener("click", listCampaigns);
  $("character-list").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-character-id]");
    if (button) chooseCharacter(button.dataset.characterId);
  });
  $("character-create").addEventListener("click", createCharacter);
  $("create-name").addEventListener("keydown", (event) => {
    if (event.key === "Enter") createCharacter();
  });
  $("character-approve").addEventListener("click", approve);
  $("settings-change-character").addEventListener("click", changeCharacter);
  $("settings-archive").addEventListener("click", askArchive);
  $("archive-yes").addEventListener("click", archive);
  $("archive-no").addEventListener("click", () => {
    state.confirmingArchive = false;
    render();
  });
  $("unconfirmed-resend").addEventListener("click", () => resend(state.unconfirmed, true));
  $("unconfirmed-edit").addEventListener("click", editUnconfirmed);
  $("console-resend").addEventListener("click", () => resend(resendTarget(), false));
  $("settings-disconnect").addEventListener("click", () => disconnect("Disconnected."));
  $("settings-change").addEventListener("click", () => {
    disconnect(`Disconnected. ${LOBBY ? "Log in" : "Join"} with the other token.`);
    $("settings-token").focus();
  });
  render();
}

start();
