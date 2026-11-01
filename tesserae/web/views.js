// Read-only renderings of a participant's view (docs/designs/browser_contract.md
// section 10). The host's projection is the only source: nothing here decides
// or offers an action. Every string is put into the page as text.

const MAX_DEPTH = 6;
const MAX_ENTRIES = 200;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function isEmpty(view) {
  return view === null || typeof view !== "object" || Object.keys(view).length === 0;
}

// The view as page content: a readable panel for a module this page knows,
// otherwise a generic tree of the view's fields.
export function viewNode(module, view) {
  if (isEmpty(view)) return element("p", "Nothing to show yet.", "note");
  if (module && module.id === "relay" && isRelayView(view)) return relayView(view);
  return tree(view, 0);
}

function isRelayView(view) {
  const scene = view.scene;
  return scene !== null && typeof scene === "object" && "character" in view;
}

// Relay: the scene and the player's own worker. `commands` and `rules` are
// left out: players talk to the referee instead.
function relayView(view) {
  const box = element("div", undefined, "relay-view");
  const scene = view.scene;
  box.append(element("h3", String(scene.title ?? "The scene")));
  if (scene.description !== undefined) box.append(element("p", String(scene.description)));
  box.append(
    element("p", scene.restored ? "The relay is restored." : "The relay is still offline.", "relay-state"),
  );
  box.append(element("h3", "Your worker"));
  const worker = view.character;
  if (worker === null || typeof worker !== "object") {
    box.append(element("p", "No worker yet.", "note"));
  } else {
    box.append(facts([["Name", worker.name], ["Repair", worker.repair], ["Effort", worker.effort]]));
  }
  return box;
}

function facts(pairs) {
  const list = element("dl", undefined, "facts");
  for (const [label, value] of pairs) {
    list.append(element("dt", label), leaf("dd", value));
  }
  return list;
}

function leaf(tag, value) {
  if (value === null || value === undefined) return element(tag, "none", "none");
  return element(tag, String(value));
}

// Any JSON value: objects as labelled lists, arrays as ordered lists, at most
// MAX_DEPTH levels and MAX_ENTRIES entries per level.
function tree(value, depth) {
  if (value === null || typeof value !== "object") return leaf("span", value);
  if (depth >= MAX_DEPTH) return element("span", "…", "none");
  const entries = Array.isArray(value)
    ? value.map((item, index) => [index, item])
    : Object.entries(value);
  const shown = entries.slice(0, MAX_ENTRIES);
  const more = entries.length - shown.length;
  if (Array.isArray(value)) {
    const list = element("ol", undefined, "tree-list");
    if (shown.length === 0) return element("span", "none", "none");
    for (const [, item] of shown) {
      const row = element("li");
      row.append(tree(item, depth + 1));
      list.append(row);
    }
    if (more > 0) list.append(element("li", `${more} more`, "none"));
    return list;
  }
  if (shown.length === 0) return element("span", "none", "none");
  const list = element("dl", undefined, "tree");
  for (const [key, item] of shown) {
    const description = element("dd");
    description.append(tree(item, depth + 1));
    list.append(element("dt", key), description);
  }
  if (more > 0) list.append(element("dt", `${more} more`, "none"), element("dd"));
  return list;
}
