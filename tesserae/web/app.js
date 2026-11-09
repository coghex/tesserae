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
//
// In a campaign with a campaign host (docs/designs/governance_contract.md
// section 15) Settings shows the role and the rules, and to the host its
// requests, rules, administration and inspection. Host data lives only in
// `state.host`, under its own scope: it is cleared, and late replies dropped,
// when the role is no longer host, on leaving, on lost access or identity,
// and on any reconnect until the host says the page is still its host.

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
  role: null, // "host" or "guest" in a campaign with a campaign host
  rules: null, // the rules as this connection's character sees them (rules_view)
  requestsNote: "", // the Console's note on the player's own requests
  // Negotiated creation overrides: the campaign's categories and their
  // policy (null in a campaign that has none), and the player's own
  // requests and the exact offers on them.
  overrides: null,
  own: emptyOwn(),
  host: emptyHost(), // the campaign host's data: never shown to anyone else
};

function emptyHost() {
  return {
    requests: null, // every request (requests)
    rules: null, // the revisions (rules_list)
    shown: null, // one revision's document (rules_export)
    confirmActivate: null, // {revision, expected} awaiting Activate
    operations: null, // host_operations
    pendingCommand: null, // {command, args, revision} awaiting Run it
    inspect: null, // host_view
    messages: [], // host_messages pages, in order
    nextAfter: null, // the next page's after_seq, or null at the end
    messagesShown: false,
    audit: null, // host_audit records, as read so far
    auditNext: null, // the next page's `after`, or null at the end
    confirmTransfer: null, // the player ID awaiting Hand it over
    note: "",
    retry: null, // {message, describe, onReply}: a change to resend exactly
    overrides: null, // open override requests (override_requests)
    overridesNext: null, // the next page's cursor, or null at the end
    reviews: new Map(), // proposal ID to the request as last reviewed
    counter: null, // {proposalId, characterId, intent, preview, digest} being drafted
    qualities: null, // what can change on the override panel's draft
    plan: null, // {characterId, intent, preview, digest}: a reviewed direct override
    version: 0, // bumped whenever an override list or review changes
  };
}

function emptyOwn() {
  return {
    items: null, // the player's override requests, as read so far
    next: null, // the next page's cursor, or null at the end
    offers: new Map(), // offer ID to the exact offer, for open offers
    confirm: null, // {offerId, revision, digest} awaiting Accept
    revise: null, // {proposalId, characterId} for a deliberate revision
    note: "",
    version: 0, // bumped whenever any of these change
  };
}

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
let hostScope = {}; // replaced whenever the host data is cleared
let renderedHost = {}; // the host objects each list shows
let renderedOwnRequests = null; // the requests the Console shows
let renderedOwn = null; // the override requests' version the Console shows

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
  clearHost(); // until the host says this page is its host again
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
  if (LOBBY) {
    loadRules();
    loadOwnOverrides();
    if (state.role === "host") loadHostOverview();
  }
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
    clearHost(); // privileged data never outlives the connection
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
    role: null,
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
    rules: null,
    requestsNote: "",
    overrides: null,
    own: emptyOwn(),
  });
  statusRead = null;
  syncRead = null;
  $("composer-text").value = "";
  $("composer-private").checked = false;
  $("override-revise-intent").value = "";
  $("override-revise-explanation").value = "";
  $("console-override-list").replaceChildren();
  renderedOwn = null;
  clearHost();
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
    rules: null,
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
  // The rules are the actor's: read them for the new one, once it is set.
  if (changed && state.phase === "joined") queueMicrotask(loadRules);
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
  state.role = snapshot.role ?? null; // the campaign role, with a campaign host
  state.overrides = Array.isArray(snapshot.override_categories)
    ? snapshot.override_categories
    : null;
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
  } else if (message.type === "role") {
    const was = state.role;
    state.role = message.role;
    if (message.role !== "host") {
      clearHost(was === "host" ? "You are no longer this campaign's host." : "");
    } else if (was !== "host") {
      loadHostOverview();
    }
    loadRules();
  } else if (message.type === "proposal") {
    // A request's state changed: the player's own, or one for the host. An
    // approval may have given a shown draft a new revision: read it again.
    if (state.referee) readStatus();
    if (state.actor && state.actor.mode === "creation") readView();
    if (state.role === "host") loadRequests();
    loadRules();
    loadOwnOverrides();
    if (state.role === "host") loadHostOverrides();
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
  // A job may have asked the host for an override: read the list again.
  loadOwnOverrides();
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

// ---- Campaign governance (docs/designs/governance_contract.md section 15) --

// Clears every host datum and makes replies to anything asked before land
// nowhere. The page never decides authority: the host refuses, and the
// page then shows nothing of it.
function clearHost(note = "") {
  hostScope = {};
  state.host = emptyHost();
  state.host.note = note;
  renderedHost = {};
  $("host-admin-args").value = "";
  $("host-rules-file").value = "";
  $("host-transfer-player").value = "";
  for (const id of ["host-counter-intent", "host-counter-note", "host-override-character",
    "host-override-intent", "host-override-reason"]) {
    $(id).value = "";
  }
  for (const id of ["host-request-list", "host-revision-list", "host-inspect-body",
    "host-message-list", "host-admin-operation", "host-audit-list", "host-override-list",
    "host-counter-preview", "host-override-qualities", "host-override-preview"]) {
    $(id).replaceChildren();
  }
}

// One governance request from the live campaign; null if the page has left
// it (or, with `host`, cleared its host data) since, or the host refused
// because the sender is not its campaign host (and its host data is gone).
async function ask(send, { host = false } = {}) {
  const current = session;
  const campaignHere = campaignScope;
  const here = hostScope;
  if (!current || state.phase !== "joined") return null;
  let reply;
  try {
    reply = await send(current);
  } catch {
    return null; // the connection closed; lost() has already said so
  }
  if (!stillIn(current, campaignHere) || leftCampaign(reply)) return null;
  if (host && here !== hostScope) return null;
  if (reply.type === "error" && reply.code === "not_campaign_host") {
    clearHost("Only the campaign host can do that, and you are not its host here now.");
    render();
    return null;
  }
  return reply;
}

// The rules as this connection's character (or, with none, any new
// character) sees them: never another character's exception.
async function loadRules() {
  if (!LOBBY) return;
  const here = scope; // the actor asked for: a reply for an earlier one is dropped
  const asked = actorKey(state.actor);
  const character = state.actor && state.actor.character;
  const wanted = character ? character.character_id : null;
  const reply = await ask((current) => current.rulesView(wanted));
  if (!reply || here !== scope || actorKey(state.actor) !== asked) return;
  if (reply.type === "rules_view" && (reply.character_id ?? null) === wanted) {
    state.rules = reply;
    render();
  }
}

function loadHostOverview() {
  loadRequests();
  loadRevisions();
  loadHostOverrides();
}

async function loadRequests() {
  const reply = await ask((current) => current.requests(), { host: true });
  if (reply && reply.type === "requests" && state.role === "host") {
    state.host.requests = reply.requests;
    render();
  }
}

async function loadRevisions() {
  const reply = await ask((current) => current.rulesList(), { host: true });
  if (reply && reply.type === "rules_list") {
    state.host.rules = reply;
    render();
  }
}

async function showRevision(revision) {
  const reply = await ask((current) => current.rulesExport(revision), { host: true });
  if (reply && reply.type === "rules") state.host.shown = reply;
  else if (reply) state.host.note = reply.message;
  render();
}

// One host change, sent as exactly `message`. Refused as busy or failed
// with nothing stored, it can be resent unchanged (Retry exactly) until
// anything else is done or the host data is cleared.
async function change(message, describe, onReply) {
  state.host.note = "";
  state.host.retry = null;
  render();
  const reply = await ask((current) => current.request(message), { host: true });
  if (!reply) return;
  if (reply.type === "error" && ["referee_busy", "internal_error"].includes(reply.code)) {
    state.host.retry = { message, describe, onReply };
    state.host.note = `${reply.message} Nothing changed.`;
  } else {
    onReply(reply);
  }
  render();
}

function retryChange() {
  const retry = state.host.retry;
  if (retry) change(retry.message, retry.describe, retry.onReply);
}

function decide(proposalId, decision) {
  const field = $("host-request-list").querySelector(
    `input[data-proposal-id="${CSS.escape(proposalId)}"]`,
  );
  const note = field ? field.value.trim() : "";
  const message = {
    version: 1, type: "request_decide", request_id: newRequestId(),
    proposal_id: proposalId, decision, note: note || null,
  };
  const verb = decision === "approve" ? "Approve" : "Deny";
  change(message, `${verb} request ${proposalId}`, (reply) => {
    if (reply.type === "proposal" && reply.state === "stale") {
      state.host.note =
        `Request ${proposalId} is stale: the game changed since it was asked, so ` +
        "nothing changed. A new request is needed.";
    } else if (reply.type === "proposal") {
      state.host.note = `Request ${proposalId} is now ${reply.state}.`;
    } else {
      state.host.note = reply.message || "The host could not decide it.";
    }
    loadRequests();
    loadRevisions();
    loadRules();
    if (state.actor) listCharacters();
  });
}

function askActivate(revision) {
  const rules = state.host.rules;
  if (!rules) return;
  state.host.confirmActivate = { revision, expected: rules.policy_revision };
  render();
  $("host-activate-no").focus();
}

function activate() {
  const confirm = state.host.confirmActivate;
  if (!confirm) return;
  state.host.confirmActivate = null;
  const message = {
    version: 1, type: "rules_activate", request_id: newRequestId(),
    revision: confirm.revision, expected_policy_revision: confirm.expected,
  };
  change(message, `Activate revision ${confirm.revision}`, (reply) => {
    if (reply.type === "rules") {
      state.host.note = `Revision ${reply.revision} is now the campaign's settings.`;
    } else if (reply.code === "stale_policy") {
      state.host.note =
        "The settings changed since you looked, so nothing changed. Review the " +
        "revisions again.";
    } else {
      state.host.note = problemsText(reply);
    }
    loadRevisions();
    loadRules();
    if (state.actor) listCharacters();
  });
}

function reject(revision) {
  const message = { version: 1, type: "rules_reject", request_id: newRequestId(), revision };
  change(message, `Reject revision ${revision}`, (reply) => {
    state.host.note = reply.type === "rules"
      ? `Revision ${reply.revision} is rejected.`
      : reply.message;
    loadRevisions();
  });
}

async function importRules() {
  const file = $("host-rules-file").files && $("host-rules-file").files[0];
  if (!file) {
    state.host.note = "Choose a JSON settings document first.";
    render();
    return;
  }
  const here = hostScope;
  let content;
  try {
    content = JSON.parse(await file.text());
  } catch {
    state.host.note = "That file is not a JSON document.";
    render();
    return;
  }
  if (here !== hostScope) return;
  const message = { version: 1, type: "rules_import", request_id: newRequestId(), content };
  change(message, `Import ${file.name}`, (reply) => {
    state.host.note = reply.type === "rules"
      ? `Stored as candidate revision ${reply.revision}; nothing live changed.`
      : problemsText(reply);
    $("host-rules-file").value = "";
    loadRevisions();
  });
}

function problemsText(reply) {
  const problems = Array.isArray(reply.problems) ? reply.problems.map(String) : [];
  return [reply.message || "The host refused it.", ...problems].join(" ");
}

async function loadOperations() {
  const reply = await ask((current) => current.hostOperations(), { host: true });
  if (reply && reply.type === "host_operations") {
    state.host.operations = reply;
    render();
    chooseOperation();
  }
}

// Prefills the arguments with what the module describes: a starting point
// to edit, never a validator. The host checks them when it runs.
function chooseOperation() {
  const operations = (state.host.operations && state.host.operations.operations) || [];
  const chosen = operations.find((op) => op.command === $("host-admin-operation").value);
  $("host-admin-description").textContent = chosen ? String(chosen.description ?? "") : "";
  const args = {};
  for (const arg of (chosen && Array.isArray(chosen.args) ? chosen.args : [])) {
    if (!arg || typeof arg.name !== "string") continue;
    args[arg.name] = Array.isArray(arg.values) && arg.values.length ? arg.values[0]
      : arg.type === "integer" ? (Number.isInteger(arg.min) ? arg.min : 0)
        : arg.type === "boolean" ? false
          : String(arg.type || "").startsWith("list") ? []
            : "";
  }
  $("host-admin-args").value = chosen ? JSON.stringify(args, null, 2) : "";
  state.host.pendingCommand = null;
  render();
}

// Shows exactly what will run, at the revision the page shows now.
function reviewCommand() {
  const command = $("host-admin-operation").value;
  let args;
  try {
    args = JSON.parse($("host-admin-args").value || "{}");
  } catch {
    args = null;
  }
  if (!command || args === null || typeof args !== "object" || Array.isArray(args)) {
    state.host.note = "The arguments must be one JSON object.";
    render();
    return;
  }
  state.host.pendingCommand = { command, args, revision: state.revision };
  state.host.note = "";
  render();
  $("host-admin-cancel").focus();
}

function runCommand() {
  const pending = state.host.pendingCommand;
  if (!pending) return;
  state.host.pendingCommand = null;
  const message = {
    version: 1, type: "host_command", request_id: newRequestId(),
    expected_revision: pending.revision, command: pending.command, args: pending.args,
  };
  change(message, `Run ${pending.command}`, (reply) => {
    if (reply.type === "result") {
      state.host.note = `Done at revision ${reply.revision}.`;
      readView();
      if (state.host.inspect) loadInspect();
    } else if (reply.code === "stale_revision") {
      state.host.note =
        "The game moved on since you reviewed this, so nothing changed. Review it again.";
      readView();
    } else {
      state.host.note = reply.message || "The host could not run it.";
    }
  });
}

async function loadInspect() {
  const reply = await ask((current) => current.hostView(), { host: true });
  if (reply && reply.type === "host_view") {
    state.host.inspect = reply;
    render();
  }
}

async function loadMessages(more) {
  const after = more ? state.host.nextAfter : 0;
  if (after === null) return;
  const reply = await ask((current) => current.hostMessages(after), { host: true });
  if (reply && reply.type === "host_messages") {
    state.host.messages = more ? [...state.host.messages, ...reply.messages] : reply.messages;
    state.host.nextAfter = reply.next_after_seq;
    state.host.messagesShown = true;
    render();
  }
}

// The audit, oldest first, a page at a time: Show more reads the next.
async function loadAudit(more) {
  const after = more ? state.host.auditNext : 0;
  if (after === null) return;
  const reply = await ask((current) => current.hostAudit(after), { host: true });
  if (reply && reply.type === "host_audit") {
    state.host.audit = more ? [...(state.host.audit || []), ...reply.records] : reply.records;
    state.host.auditNext = reply.next_after;
    render();
  }
}

function askTransfer() {
  const player = $("host-transfer-player").value.trim();
  if (!player) {
    state.host.note = "Type the ID of the player to hand the campaign to.";
    render();
    return;
  }
  state.host.confirmTransfer = player;
  render();
  $("host-transfer-no").focus();
}

// Hands the campaign over; the host's answer makes this page a guest, and
// its host data goes with that role.
function transfer() {
  const player = state.host.confirmTransfer;
  if (!player) return;
  state.host.confirmTransfer = null;
  const message = {
    version: 1, type: "campaign_host_transfer", request_id: newRequestId(), player_id: player,
  };
  change(message, `Hand the campaign to ${player}`, (reply) => {
    if (reply.type === "role") {
      state.role = reply.role;
      clearHost(`You handed the campaign to ${player}; you are a guest here now.`);
      loadRules();
    } else {
      state.host.note = reply.message || "The host could not hand it over.";
    }
  });
}

// The player's own request, withdrawn: its only control.
async function withdraw(proposalId) {
  const reply = await ask((current) =>
    current.request({
      version: 1, type: "request_cancel", request_id: newRequestId(), proposal_id: proposalId,
    }),
  );
  if (!reply) return;
  state.requestsNote = reply.type === "proposal"
    ? `Request ${proposalId} is withdrawn.`
    : reply.message || "The host could not withdraw it.";
  readStatus();
  render();
}

// ---- Negotiated creation overrides (creation overrides contract section 12)
// The player's requests and the exact offers on them live in `state.own`;
// the host's reviews, counteroffer and direct override in `state.host`,
// cleared with the rest of its data. Every acceptance, offer and direct
// override sends the digest of the preview this page showed, so a change
// meanwhile is refused and shown, never applied in its place.

function overrideRefusal(reply) {
  if (!reply || reply.type !== "error") return "";
  const problems = Array.isArray(reply.problems) && reply.problems.length
    ? ` ${reply.problems.map(String).join(" ")}`
    : "";
  if (reply.code === "offer_stale" || reply.code === "offer_unavailable") {
    return "That offer is no longer open, so nothing was applied. Look at the request " +
      "again.";
  }
  if (reply.code === "stale_preview") {
    return "What it would do changed since you reviewed it, so nothing was applied. " +
      "Review it again.";
  }
  return `${reply.message}${problems}`;
}

function parseIntent(text) {
  try {
    const intent = JSON.parse(text);
    if (intent && typeof intent === "object" && !Array.isArray(intent)) return intent;
  } catch {
    // shown below
  }
  return null;
}

// Whose response a request waits for, in words: an offer is never applied.
function awaitedText(request, asHost) {
  const offer = request.offer;
  if (request.state === "pending_host") return "Waiting for the host";
  if (request.state === "awaiting_player" && offer) {
    if (!offer.current) {
      return `Offer ${offer.offer_revision} no longer holds (${offer.stale_reason || "stale"})`;
    }
    return `Offer ${offer.offer_revision} waiting for ${asHost ? "the player" : "you"}: ` +
      "offered, not applied yet";
  }
  if (request.state === "applied") return "Applied";
  return `${request.state[0].toUpperCase()}${request.state.slice(1)}`;
}

async function loadOwnOverrides(more = false) {
  if (!state.overrides || state.phase !== "joined") return;
  const message = { version: 1, type: "override_requests", states: "all" };
  if (more) {
    if (!state.own.next) return;
    message.after = state.own.next;
  }
  const here = campaignScope;
  const reply = await ask((current) => current.request(message));
  if (!reply || here !== campaignScope || reply.type !== "override_requests") return;
  const own = state.own;
  own.items = more ? [...(own.items || []), ...reply.items] : reply.items;
  own.next = reply.next;
  own.version += 1;
  for (const request of reply.items) {
    const offer = request.offer;
    if (request.state === "awaiting_player" && offer && offer.current) {
      loadOwnOffer(offer.offer_id);
    }
  }
  render();
}

async function loadOwnOffer(offerId) {
  const here = campaignScope;
  const reply = await ask((current) =>
    current.request({ version: 1, type: "override_offer", offer_id: offerId }));
  if (!reply || here !== campaignScope || reply.type !== "override_offer") return;
  state.own.offers.set(offerId, reply.offer);
  state.own.version += 1;
  render();
}

function askAccept(offerId) {
  const offer = state.own.offers.get(offerId);
  if (!offer) return;
  state.own.confirm = {
    offerId, revision: offer.offer_revision, digest: offer.preview_digest,
  };
  render();
  $("override-accept-no").focus();
}

async function acceptOffer() {
  const confirm = state.own.confirm;
  if (!confirm) return;
  state.own.confirm = null;
  render();
  const reply = await ask((current) => current.request({
    version: 1, type: "override_respond", request_id: newRequestId(),
    offer_id: confirm.offerId, response: "accept", preview_digest: confirm.digest,
  }));
  if (!reply) return;
  state.own.note = reply.type === "proposal" && reply.state === "applied"
    ? `Offer ${confirm.revision} is applied to your draft.`
    : overrideRefusal(reply);
  state.own.version += 1;
  loadOwnOverrides();
  readView();
  render();
}

async function declineOffer(offerId) {
  const field = $("console-override-list").querySelector(
    `input[data-decline-note="${CSS.escape(offerId)}"]`,
  );
  const note = field ? field.value.trim() : "";
  const reply = await ask((current) => current.request({
    version: 1, type: "override_respond", request_id: newRequestId(), offer_id: offerId,
    response: "decline", note: note || null,
  }));
  if (!reply) return;
  state.own.note = reply.type === "proposal" ? "You declined that offer." : overrideRefusal(reply);
  state.own.version += 1;
  loadOwnOverrides();
  render();
}

async function withdrawOverride(proposalId) {
  await withdraw(proposalId);
  loadOwnOverrides();
}

async function startRevise(proposalId) {
  const reply = await ask((current) =>
    current.request({ version: 1, type: "override_detail", proposal_id: proposalId }));
  if (!reply) return;
  if (reply.type !== "override_detail") {
    state.own.note = overrideRefusal(reply);
  } else {
    state.own.revise = { proposalId, characterId: reply.request.target.id };
    $("override-revise-intent").value = JSON.stringify(reply.request.intent, null, 2);
    $("override-revise-explanation").value = "";
  }
  state.own.version += 1;
  render();
  if (state.own.revise) $("override-revise-explanation").focus();
}

async function sendRevise() {
  const revise = state.own.revise;
  if (!revise) return;
  const intent = parseIntent($("override-revise-intent").value);
  const explanation = $("override-revise-explanation").value.trim();
  if (!intent || !explanation) {
    state.own.note = intent
      ? "Say why you are asking."
      : "What to change must be one JSON object.";
    render();
    return;
  }
  const reply = await ask((current) => current.request({
    version: 1, type: "override_request", request_id: newRequestId(),
    character_id: revise.characterId, intent, explanation, revises: revise.proposalId,
  }));
  if (!reply) return;
  if (reply.type === "proposal") {
    state.own.note = `Asked again: request ${reply.proposal_id} waits for the host.`;
    state.own.revise = null;
  } else {
    state.own.note = overrideRefusal(reply);
  }
  state.own.version += 1;
  loadOwnOverrides();
  render();
}

async function loadHostOverrides(more = false) {
  if (!state.overrides || state.role !== "host") return;
  const message = { version: 1, type: "override_requests", states: "open" };
  if (more) {
    if (!state.host.overridesNext) return;
    message.after = state.host.overridesNext;
  }
  const reply = await ask((current) => current.request(message), { host: true });
  if (!reply || reply.type !== "override_requests" || state.role !== "host") return;
  const host = state.host;
  host.overrides = more ? [...(host.overrides || []), ...reply.items] : reply.items;
  host.overridesNext = reply.next;
  host.version += 1;
  render();
}

async function reviewOverride(proposalId) {
  const reply = await ask((current) =>
    current.request({ version: 1, type: "override_detail", proposal_id: proposalId }),
  { host: true });
  if (!reply) return;
  if (reply.type === "override_detail") state.host.reviews.set(proposalId, reply.request);
  else state.host.note = overrideRefusal(reply);
  state.host.version += 1;
  render();
}

function hostNote(proposalId) {
  const field = $("host-override-list").querySelector(
    `input[data-override-note="${CSS.escape(proposalId)}"]`,
  );
  return field ? field.value.trim() : "";
}

function afterVerdict(proposalId, done) {
  return (reply) => {
    state.host.note = reply.type === "proposal" ? done(reply) : overrideRefusal(reply);
    state.host.reviews.delete(proposalId);
    state.host.version += 1;
    loadHostOverrides();
  };
}

function offerOverride(proposalId) {
  const request = state.host.reviews.get(proposalId);
  const current = request && request.current;
  if (!current || !["same", "changed"].includes(current.status)) return;
  const note = hostNote(proposalId);
  const message = {
    version: 1, type: "override_decide", request_id: newRequestId(),
    proposal_id: proposalId, decision: "offer", preview_digest: current.preview_digest,
    note: note || null,
  };
  change(message, `Offer request ${proposalId}`, afterVerdict(proposalId, (reply) =>
    `Request ${proposalId} is offered (offer ${reply.offer_revision}). Nothing applies ` +
    "until the player accepts."));
}

function denyOverride(proposalId) {
  const note = hostNote(proposalId);
  if (!note) {
    state.host.note = "A denial says why: write a note first.";
    render();
    return;
  }
  const message = {
    version: 1, type: "override_decide", request_id: newRequestId(),
    proposal_id: proposalId, decision: "deny", note,
  };
  change(message, `Deny request ${proposalId}`,
    afterVerdict(proposalId, () => `Request ${proposalId} is denied.`));
}

function withdrawOffer(proposalId, offerId) {
  const note = hostNote(proposalId);
  if (!note) {
    state.host.note = "Withdrawing an offer says why: write a note first.";
    render();
    return;
  }
  const message = {
    version: 1, type: "override_withdraw", request_id: newRequestId(), offer_id: offerId, note,
  };
  change(message, `Withdraw offer ${offerId}`, afterVerdict(proposalId, () =>
    `Offer ${offerId} is withdrawn; anything already accepted stays applied.`));
}

function startCounter(proposalId) {
  const request = state.host.reviews.get(proposalId);
  if (!request) return;
  state.host.counter = {
    proposalId, characterId: request.target.id, intent: null, preview: null, digest: null,
  };
  $("host-counter-intent").value = JSON.stringify(request.intent, null, 2);
  $("host-counter-note").value = "";
  render();
  $("host-counter-intent").focus();
}

async function reviewPreview(characterId, intent) {
  return ask((current) => current.request({
    version: 1, type: "override_preview", character_id: characterId, intent,
  }), { host: true });
}

async function reviewCounter() {
  const counter = state.host.counter;
  if (!counter) return;
  const intent = parseIntent($("host-counter-intent").value);
  if (!intent) {
    state.host.note = "What to offer must be one JSON object.";
    render();
    return;
  }
  const reply = await reviewPreview(counter.characterId, intent);
  if (!reply || state.host.counter !== counter) return;
  if (reply.type === "override_preview") {
    Object.assign(counter, { intent, preview: reply.preview, digest: reply.preview_digest });
  } else {
    Object.assign(counter, { intent: null, preview: null, digest: null });
    state.host.note = overrideRefusal(reply);
  }
  state.host.version += 1;
  render();
}

function sendCounter() {
  const counter = state.host.counter;
  if (!counter || !counter.digest) return;
  const note = $("host-counter-note").value.trim();
  if (!note) {
    state.host.note = "A counteroffer says why: write it first.";
    render();
    return;
  }
  const message = {
    version: 1, type: "override_decide", request_id: newRequestId(),
    proposal_id: counter.proposalId, decision: "counteroffer",
    preview_digest: counter.digest, intent: counter.intent, note,
  };
  state.host.counter = null;
  change(message, `Counteroffer request ${counter.proposalId}`,
    afterVerdict(counter.proposalId, (reply) =>
      `Request ${counter.proposalId} has counteroffer ${reply.offer_revision}. Nothing ` +
      "applies until the player accepts."));
}

async function discoverOverride() {
  const characterId = $("host-override-character").value.trim();
  if (!characterId) return;
  const reply = await ask((current) => current.request({
    version: 1, type: "override_discover", character_id: characterId, query: {},
  }), { host: true });
  if (!reply) return;
  if (reply.type === "override_discover") state.host.qualities = reply.items;
  else state.host.note = overrideRefusal(reply);
  state.host.version += 1;
  render();
}

async function reviewOverridePlan() {
  const characterId = $("host-override-character").value.trim();
  const intent = parseIntent($("host-override-intent").value);
  state.host.plan = null;
  if (!characterId || !intent) {
    state.host.note = "Name the draft, and give what to change as one JSON object.";
    render();
    return;
  }
  const reply = await reviewPreview(characterId, intent);
  if (!reply) return;
  if (reply.type === "override_preview") {
    state.host.plan = {
      characterId, intent, preview: reply.preview, digest: reply.preview_digest,
    };
  } else {
    state.host.note = overrideRefusal(reply);
  }
  state.host.version += 1;
  render();
}

function applyOverridePlan() {
  const plan = state.host.plan;
  if (!plan) return;
  const reason = $("host-override-reason").value.trim();
  if (!reason) {
    state.host.note = "A direct change says why: write a reason first.";
    render();
    return;
  }
  const message = {
    version: 1, type: "override_apply", request_id: newRequestId(),
    character_id: plan.characterId, intent: plan.intent, preview_digest: plan.digest, reason,
  };
  state.host.plan = null;
  change(message, `Apply the change to ${plan.characterId}`, (reply) => {
    state.host.note = reply.type === "override_applied"
      ? `Applied to ${reply.character_id} at revision ${reply.revision}.`
      : overrideRefusal(reply);
    state.host.version += 1;
  });
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

// Who sees a creation besides its player: a guest's campaign host may read
// it (docs/designs/governance_contract.md section 7).
function privateTo() {
  return state.role === "guest" ? "you and the campaign host" : "you";
}

// Who this connection acts as, for the status line.
function actorText() {
  const actor = state.actor;
  if (!actor) return "";
  if (actor.mode === "play") return `${actor.character.name} · `;
  if (actor.mode === "creation") {
    return `Creating ${characterLabel(actor.character)} · private to ${privateTo()} · `;
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
  renderOwnRequests(shown);
  renderOwnOverrides(shown);
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
    ? `Draft revision ${state.sheetRevision}. ${state.role === "guest" ? "Only you and the campaign host see this." : "Only you see this."}`
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
  renderRules(joined);
  renderHost(joined);
}

// ---- Rendering campaign governance ----------------------------------------

function textItem(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(label, data) {
  const node = document.createElement("button");
  node.type = "button";
  node.textContent = label;
  Object.assign(node.dataset, data);
  return node;
}

function compact(value) {
  return JSON.stringify(value);
}

function requestSummary(request) {
  const target = request.target;
  const on = target ? `${target.kind} ${target.id}` : "the campaign";
  const who = (request.requester && (request.requester.name || request.requester.participant_id)) || "?";
  return `${who} asks for ${request.kind} ${compact(request.payload)} on ${on}: “${request.explanation}”`;
}

// The Console: the player's own requests and what became of them.
function renderOwnRequests(shown) {
  const requests = (shown && state.referee && state.referee.requests) || [];
  $("console-requests").hidden = requests.length === 0 && !state.requestsNote;
  $("console-requests-note").textContent = state.requestsNote;
  if (renderedOwnRequests !== requests) {
    renderedOwnRequests = requests;
    $("console-request-list").replaceChildren(
      ...requests.map((request) => {
        const item = document.createElement("li");
        item.dataset.proposalId = String(request.proposal_id);
        item.append(textItem("p", `${request.state}: ${request.kind} ${compact(request.payload)}`, "request-state"));
        if (request.decision_note) {
          item.append(textItem("p", `The host's note: ${request.decision_note}`, "note"));
        }
        if (request.state === "pending") {
          item.append(button("Withdraw", { withdraw: String(request.proposal_id) }));
        }
        return item;
      }),
    );
  }
  const usable = state.phase === "joined";
  for (const node of $("console-request-list").querySelectorAll("button")) node.disabled = !usable;
}

// A preview, every part of it, as text: what would change, from what to
// what, and what it waives, allows, needs later, causes and cannot do.
function previewNodes(preview, digest) {
  if (!preview) return [];
  const nodes = [textItem("p", String(preview.headline ?? ""), "preview-headline")];
  const list = document.createElement("ul");
  list.className = "preview";
  for (const effect of preview.effects || []) {
    list.append(textItem("li", `${effect.quality_id}: ${compact(effect.before)} → ` +
      `${compact(effect.after)} (${effect.summary})`));
  }
  for (const waiver of preview.waivers || []) {
    list.append(textItem("li", `Waives, ${waiver.creation_only ? "until approval" :
      "lasting"}: ${waiver.quality_id}: ${waiver.summary}`));
  }
  for (const allowance of preview.allowances || []) {
    list.append(textItem("li", `Allows, until approval: ${allowance.summary}`));
  }
  for (const step of preview.later_steps || []) {
    list.append(textItem("li", `Later: ${step.command}: ${step.summary} (${step.stakes})`));
  }
  for (const text of preview.consequences || []) list.append(textItem("li", `Then: ${text}`));
  for (const text of preview.limitations || []) {
    list.append(textItem("li", `Cannot: ${text}`));
  }
  nodes.push(list);
  if (digest) nodes.push(textItem("p", `Preview ${String(digest).slice(0, 12)}`, "note"));
  return nodes;
}

// The Console: the player's override requests, whose response each waits
// for, and an open offer shown exactly, under "Offered, not applied yet".
function renderOwnOverrides(shown) {
  const own = state.own;
  const on = Boolean(shown && state.overrides);
  const items = own.items || [];
  $("console-overrides").hidden = !on || (!items.length && !own.note && !own.revise);
  $("console-overrides-note").textContent = own.note;
  $("console-overrides-more").hidden = !own.next;
  if (renderedOwn !== own.version) {
    renderedOwn = own.version;
    $("console-override-list").replaceChildren(...items.map((request) => {
      const item = document.createElement("li");
      const id = String(request.proposal_id);
      item.dataset.proposalId = id;
      item.append(textItem("p", `${awaitedText(request, false)}: ${request.target.id} ` +
        `(${(request.categories || []).join(", ")}): “${request.explanation}”`,
      "request-state"));
      if (request.decision_note) {
        item.append(textItem("p", `The host's note: ${request.decision_note}`, "note"));
      }
      const offer = request.offer && own.offers.get(request.offer.offer_id);
      if (request.state === "awaiting_player" && request.offer && request.offer.current && offer) {
        const box = document.createElement("div");
        box.className = "offer";
        box.append(textItem("h3", `Offered, not applied yet (offer ${offer.offer_revision}` +
          `${offer.origin === "counteroffer" ? ", a counteroffer" : ""})`));
        if (offer.note) box.append(textItem("p", `The host's note: ${offer.note}`, "note"));
        box.append(...previewNodes(offer.preview, offer.preview_digest));
        const note = document.createElement("input");
        note.type = "text";
        note.maxLength = 1000;
        note.dataset.declineNote = String(offer.offer_id);
        note.setAttribute("aria-label", `Note for declining offer ${offer.offer_revision}`);
        box.append(button("Accept…", { accept: String(offer.offer_id) }), note,
          button("Decline", { decline: String(offer.offer_id) }));
        item.append(box);
      }
      if (request.state === "pending_host" || request.state === "awaiting_player") {
        item.append(button("Withdraw request", { withdrawOverride: id }));
      } else {
        item.append(button("Revise", { revise: id }));
      }
      return item;
    }));
  }
  const confirm = own.confirm;
  $("override-accept-confirm").hidden = !confirm;
  $("override-accept-text").textContent = confirm
    ? `Accept offer ${confirm.revision} exactly as shown? It applies to your draft at ` +
      "once; approving the whole character is still separate."
    : "";
  $("override-accept-yes").textContent = confirm ? `Accept offer ${confirm.revision}` : "Accept";
  $("override-revise").hidden = !own.revise;
  $("override-revise-title").textContent = own.revise
    ? `Ask again, revising ${own.revise.proposalId}`
    : "Ask again";
  const usable = state.phase === "joined";
  for (const node of $("console-overrides").querySelectorAll("button")) node.disabled = !usable;
}

// Settings: the role and the rules, for everyone in a campaign.
function renderRules(joined) {
  const panel = $("settings-rules-panel");
  panel.hidden = !LOBBY || !joined;
  const role = state.role;
  $("settings-role").textContent =
    role === "host"
      ? "You are this campaign's host."
      : role === "guest"
        ? "You are a guest here. The campaign host can read your private creation " +
          "and conversation, but no other guest can."
        : "This campaign has no campaign host.";
  const rules = state.rules;
  if (renderedHost.rulesView === rules) return;
  renderedHost.rulesView = rules;
  const body = [];
  if (rules) {
    body.push(textItem("p", `Settings revision ${rules.policy_revision}.`));
    body.push(viewNode(null, rules.settings));
    if (rules.exception) body.push(textItem("p", `Your exception: ${compact(rules.exception)}`));
    if (rules.guidance) body.push(textItem("p", `Guidance: ${rules.guidance}`));
    if (Array.isArray(rules.override_categories) && rules.override_categories.length) {
      const list = document.createElement("ul");
      list.className = "override-categories";
      for (const category of rules.override_categories) {
        list.append(textItem("li", `Override requests for ${category.label}: ` +
          (category.policy === "blocked" ? "not taken" : "taken, for the host to decide")));
      }
      body.push(textItem("p", "Changes to a draft the host may be asked for:"), list);
    }
    if (Array.isArray(rules.requirements)) {
      const list = document.createElement("ul");
      list.className = "requirements";
      for (const entry of rules.requirements) {
        const met = entry.met === true ? ", done" : entry.met === false ? ", to do" : "";
        list.append(textItem("li", `${entry.id} (${entry.kind}${met}): ${entry.description}`));
      }
      body.push(list);
    }
  }
  $("settings-rules-body").replaceChildren(...body);
}

// Settings: the campaign host's panels, only while it is the host.
function renderHost(joined) {
  const host = state.host;
  const isHost = joined && state.role === "host";
  $("host-panels").hidden = !isHost && !host.note;
  $("host-note").textContent = host.note;
  $("host-note").hidden = !host.note;
  for (const id of ["host-requests", "host-rules", "host-admin", "host-inspect", "host-transfer"]) {
    $(id).hidden = !isHost;
  }
  renderHostOverrides(isHost && Boolean(state.overrides));
  $("host-retry").hidden = !host.retry;
  $("host-retry-text").textContent = host.retry
    ? `${host.retry.describe}: not done yet. Retry sends exactly the same request.`
    : "";

  const requests = host.requests || [];
  $("host-requests-empty").hidden = requests.length > 0;
  if (renderedHost.requests !== host.requests) {
    renderedHost.requests = host.requests;
    $("host-request-list").replaceChildren(
      ...requests.map((request) => {
        const item = document.createElement("li");
        const id = String(request.proposal_id);
        item.dataset.proposalId = id;
        item.append(textItem("p", `${id} (${request.state}): ${requestSummary(request)}`));
        if (request.decision_note) item.append(textItem("p", `Note: ${request.decision_note}`, "note"));
        if (request.state === "pending") {
          const note = document.createElement("input");
          note.type = "text";
          note.maxLength = 1000;
          note.dataset.proposalId = id;
          note.setAttribute("aria-label", `Note for ${id}`);
          item.append(note, button("Approve", { approve: id }), button("Deny", { deny: id }));
        }
        return item;
      }),
    );
  }

  const rules = host.rules;
  $("host-rules-active").textContent = rules
    ? `Active settings revision: ${rules.policy_revision || "none"}.`
    : "";
  if (renderedHost.rules !== rules) {
    renderedHost.rules = rules;
    $("host-revision-list").replaceChildren(
      ...((rules && rules.revisions) || []).map((revision) => {
        const item = document.createElement("li");
        const number = String(revision.revision);
        item.append(textItem("span", `Revision ${number}: ${revision.state} (${revision.origin}) `));
        item.append(button("Show", { show: number }));
        if (revision.state === "candidate") {
          item.append(button("Activate…", { activate: number }), button("Reject", { reject: number }));
        }
        return item;
      }),
    );
  }
  const confirm = host.confirmActivate;
  $("host-activate-confirm").hidden = !confirm;
  $("host-activate-text").textContent = confirm
    ? `Make revision ${confirm.revision} the campaign's settings, replacing revision ` +
      `${confirm.expected}? Drafts it changes must be approved again; ready characters ` +
      "are not changed."
    : "";
  const shown = host.shown;
  $("host-rules-shown").hidden = !shown;
  $("host-rules-shown-title").textContent = shown ? `Revision ${shown.revision} (${shown.state})` : "";
  $("host-rules-shown-text").textContent = shown ? JSON.stringify(shown.content, null, 2) : "";

  const operations = host.operations;
  $("host-admin-form").hidden = !operations;
  if (renderedHost.operations !== operations) {
    renderedHost.operations = operations;
    $("host-admin-operation").replaceChildren(
      ...((operations && operations.operations) || []).map((op) => {
        const option = document.createElement("option");
        option.value = String(op.command);
        option.textContent = String(op.command);
        return option;
      }),
    );
  }
  const pending = host.pendingCommand;
  $("host-admin-confirm").hidden = !pending;
  $("host-admin-confirm-text").textContent = pending
    ? `Run ${pending.command} at revision ${pending.revision}? If the game moves on ` +
      "first, it is refused and nothing changes."
    : "";
  $("host-admin-confirm-args").textContent = pending ? JSON.stringify(pending.args, null, 2) : "";

  if (renderedHost.inspect !== host.inspect) {
    renderedHost.inspect = host.inspect;
    $("host-inspect-body").replaceChildren(...inspectNodes(host.inspect));
  }
  if (renderedHost.messages !== host.messages) {
    renderedHost.messages = host.messages;
    $("host-message-list").replaceChildren(
      ...host.messages.map((message) => {
        const item = messageItem(message);
        if (Array.isArray(message.to) && message.to.length) {
          const names = message.to.map((to) => String(to.name ?? to.participant_id)).join(", ");
          item.append(textItem("p", `To ${names}`, "note"));
        }
        return item;
      }),
    );
  }
  $("host-messages-more").hidden = !host.messagesShown || host.nextAfter === null;
  if (renderedHost.audit !== host.audit) {
    renderedHost.audit = host.audit;
    $("host-audit-list").replaceChildren(
      ...(host.audit || []).map((record) =>
        textItem("li", `${record.kind} by ${record.player_id} (${record.via})` +
          `${record.target ? ` on ${record.target}` : ""}: ${compact(record.detail)}`),
      ),
    );
  }
  $("host-audit-more").hidden = host.audit === null || host.auditNext === null;
  const transferring = host.confirmTransfer;
  $("host-transfer-confirm").hidden = !transferring;
  $("host-transfer-text").textContent = transferring
    ? `Hand this campaign to ${transferring}? You become a guest at once, and these ` +
      "panels close."
    : "";
  const usable = isHost && !host.retry;
  for (const node of $("host-panels").querySelectorAll("button")) {
    if (["host-retry-send", "host-retry-drop"].includes(node.id)) continue;
    if (!usable) node.disabled = true;
    else if (node.id === "host-counter-send") node.disabled = !host.counter || !host.counter.digest;
    else if (node.id === "host-override-apply") node.disabled = !host.plan;
    else node.disabled = false;
  }
}

// Settings: the host's override requests, its counteroffer and its direct
// override, only while it is the host of a campaign that has them.
function renderHostOverrides(on) {
  const host = state.host;
  $("host-overrides").hidden = !on;
  $("host-override").hidden = !on;
  const items = host.overrides || [];
  $("host-overrides-empty").hidden = items.length > 0;
  $("host-overrides-more").hidden = !host.overridesNext;
  if (renderedHost.overrides !== host.version) {
    renderedHost.overrides = host.version;
    $("host-override-list").replaceChildren(...items.map((request) => {
      const item = document.createElement("li");
      const id = String(request.proposal_id);
      item.dataset.proposalId = id;
      const who = request.requester.name || request.requester.participant_id;
      item.append(textItem("p", `${id}: ${awaitedText(request, true)}. ${who} asks for ` +
        `${request.target.id} (${(request.categories || []).join(", ")}): ` +
        `“${request.explanation}”`));
      const reviewed = host.reviews.get(id);
      if (reviewed) {
        const current = reviewed.current || {};
        if (["same", "changed"].includes(current.status)) {
          item.append(textItem("p", "What it would do now:", "note"),
            ...previewNodes(current.preview, current.preview_digest));
        } else {
          item.append(textItem("p", current.status === "invalid"
            ? `It cannot be done now: ${(current.problems || []).join(" ")}`
            : "It cannot be checked now.", "note"));
        }
      }
      const note = document.createElement("input");
      note.type = "text";
      note.maxLength = 1000;
      note.dataset.overrideNote = id;
      note.setAttribute("aria-label", `Note for ${id}`);
      item.append(note);
      if (request.state === "pending_host") {
        item.append(button("Review", { reviewOverride: id }));
        if (reviewed && ["same", "changed"].includes((reviewed.current || {}).status)) {
          item.append(button("Offer", { offerOverride: id }));
        }
        if (reviewed) item.append(button("Counteroffer…", { counterOverride: id }));
        item.append(button("Deny", { denyOverride: id }));
      } else if (request.offer && request.offer.state === "open") {
        item.append(button("Withdraw the offer", {
          withdrawOffer: id, offerId: String(request.offer.offer_id),
        }));
        item.append(button("Review", { reviewOverride: id }));
        if (reviewed) item.append(button("Counteroffer…", { counterOverride: id }));
      }
      return item;
    }));
    const counter = host.counter;
    $("host-counter-preview").replaceChildren(
      ...(counter && counter.preview ? previewNodes(counter.preview, counter.digest) : []));
    $("host-override-qualities").replaceChildren(...(host.qualities || []).map((quality) =>
      textItem("li", `${quality.quality_id} (${quality.category_id}), now ` +
        `${compact(quality.current)}: ${(quality.actions || []).map((action) =>
          `${action.action_id} ${compact(action.parameters)}`).join("; ")}`)));
    const plan = host.plan;
    $("host-override-preview").replaceChildren(
      ...(plan ? previewNodes(plan.preview, plan.digest) : []));
  }
  const counter = host.counter;
  $("host-counter").hidden = !on || !counter;
  $("host-counter-title").textContent = counter
    ? `Counteroffer for ${counter.proposalId}`
    : "Counteroffer";
  $("host-counter-send").disabled = !counter || !counter.digest;
  $("host-override-apply").disabled = !host.plan;
}

function inspectNodes(view) {
  if (!view) return [];
  const names = new Map((view.participants || []).map((p) => [p.participant_id, p.name]));
  const nodes = [textItem("p", `As of revision ${view.revision}.`, "note")];
  const list = document.createElement("ul");
  list.className = "host-characters";
  for (const character of view.characters || []) {
    const item = document.createElement("li");
    const owner = names.get(character.participant_id) || character.participant_id;
    item.append(textItem("h3", `${characterLabel(character)}, ${character.phase}, ${owner}`));
    item.append(textItem("p", String(character.character_id), "note"));
    item.append(viewNode(null, character.sheet));
    list.append(item);
  }
  nodes.push(list, textItem("h3", "Everything the module keeps"), viewNode(null, view.inspect));
  return nodes;
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
  // Campaign governance: everything is asked of the host; nothing runs
  // until its button is pressed, and changes only after a confirmation.
  $("console-request-list").addEventListener("click", (event) => {
    const node = event.target.closest("button[data-withdraw]");
    if (node) withdraw(node.dataset.withdraw);
  });
  $("settings-rules-refresh").addEventListener("click", loadRules);
  $("host-requests-refresh").addEventListener("click", loadRequests);
  $("host-request-list").addEventListener("click", (event) => {
    const node = event.target.closest("button[data-approve], button[data-deny]");
    if (node && node.dataset.approve) decide(node.dataset.approve, "approve");
    else if (node) decide(node.dataset.deny, "deny");
  });
  $("host-rules-refresh").addEventListener("click", loadRevisions);
  $("host-revision-list").addEventListener("click", (event) => {
    const node = event.target.closest("button");
    if (!node) return;
    if (node.dataset.show) showRevision(Number(node.dataset.show));
    else if (node.dataset.activate) askActivate(Number(node.dataset.activate));
    else if (node.dataset.reject) reject(Number(node.dataset.reject));
  });
  $("host-activate-yes").addEventListener("click", activate);
  $("host-activate-no").addEventListener("click", () => {
    state.host.confirmActivate = null;
    render();
  });
  $("host-rules-import").addEventListener("click", importRules);
  $("host-admin-load").addEventListener("click", loadOperations);
  $("host-admin-operation").addEventListener("change", chooseOperation);
  $("host-admin-review").addEventListener("click", reviewCommand);
  $("host-admin-run").addEventListener("click", runCommand);
  $("host-admin-cancel").addEventListener("click", () => {
    state.host.pendingCommand = null;
    render();
  });
  $("host-inspect-load").addEventListener("click", loadInspect);
  $("host-messages-load").addEventListener("click", () => loadMessages(false));
  $("host-messages-more").addEventListener("click", () => loadMessages(true));
  $("host-audit-load").addEventListener("click", () => loadAudit(false));
  $("host-audit-more").addEventListener("click", () => loadAudit(true));
  $("host-transfer-ask").addEventListener("click", askTransfer);
  $("host-transfer-yes").addEventListener("click", transfer);
  $("host-transfer-no").addEventListener("click", () => {
    state.host.confirmTransfer = null;
    render();
  });
  // Creation overrides: the player's responses and revisions, the host's
  // verdicts, counteroffer and direct override. Editing what was reviewed
  // drops the review: only what was shown is ever sent.
  $("console-override-list").addEventListener("click", (event) => {
    const node = event.target.closest("button");
    if (!node) return;
    if (node.dataset.accept) askAccept(node.dataset.accept);
    else if (node.dataset.decline) declineOffer(node.dataset.decline);
    else if (node.dataset.withdrawOverride) withdrawOverride(node.dataset.withdrawOverride);
    else if (node.dataset.revise) startRevise(node.dataset.revise);
  });
  $("override-accept-yes").addEventListener("click", acceptOffer);
  $("override-accept-no").addEventListener("click", () => {
    state.own.confirm = null;
    render();
  });
  $("console-overrides-more").addEventListener("click", () => loadOwnOverrides(true));
  $("console-overrides-refresh").addEventListener("click", () => loadOwnOverrides());
  $("override-revise-send").addEventListener("click", sendRevise);
  $("override-revise-cancel").addEventListener("click", () => {
    state.own.revise = null;
    render();
  });
  $("host-override-list").addEventListener("click", (event) => {
    const node = event.target.closest("button");
    if (!node) return;
    if (node.dataset.reviewOverride) reviewOverride(node.dataset.reviewOverride);
    else if (node.dataset.offerOverride) offerOverride(node.dataset.offerOverride);
    else if (node.dataset.counterOverride) startCounter(node.dataset.counterOverride);
    else if (node.dataset.denyOverride) denyOverride(node.dataset.denyOverride);
    else if (node.dataset.withdrawOffer) {
      withdrawOffer(node.dataset.withdrawOffer, node.dataset.offerId);
    }
  });
  $("host-overrides-more").addEventListener("click", () => loadHostOverrides(true));
  $("host-counter-review").addEventListener("click", reviewCounter);
  $("host-counter-send").addEventListener("click", sendCounter);
  $("host-counter-cancel").addEventListener("click", () => {
    state.host.counter = null;
    state.host.version += 1;
    render();
  });
  $("host-counter-intent").addEventListener("input", () => {
    const counter = state.host.counter;
    if (counter && counter.digest) {
      Object.assign(counter, { intent: null, preview: null, digest: null });
      state.host.version += 1;
      render();
    }
  });
  $("host-override-discover").addEventListener("click", discoverOverride);
  $("host-override-review").addEventListener("click", reviewOverridePlan);
  $("host-override-apply").addEventListener("click", applyOverridePlan);
  for (const id of ["host-override-character", "host-override-intent"]) {
    $(id).addEventListener("input", () => {
      if (state.host.plan) {
        state.host.plan = null;
        state.host.version += 1;
        render();
      }
    });
  }
  $("host-retry-send").addEventListener("click", retryChange);
  $("host-retry-drop").addEventListener("click", () => {
    state.host.retry = null;
    state.host.note = "";
    render();
  });
  $("settings-disconnect").addEventListener("click", () => disconnect("Disconnected."));
  $("settings-change").addEventListener("click", () => {
    disconnect(`Disconnected. ${LOBBY ? "Log in" : "Join"} with the other token.`);
    $("settings-token").focus();
  });
  render();
}

start();
