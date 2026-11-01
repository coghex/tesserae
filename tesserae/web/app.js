// The browser page: one application state shared by both screens
// (docs/designs/browser_contract.md sections 4 and 5). Screens only render it;
// changing screens or collapsing the navigation never touches a connection.
// Strings go into the page as text only, never as markup.

const NARROW = "(max-width: 40rem)";

const state = {
  screen: "console",
  navExpanded: !window.matchMedia(NARROW).matches,
};

function render() {
  const app = document.getElementById("app");
  app.classList.toggle("collapsed", !state.navExpanded);

  const toggle = document.getElementById("nav-toggle");
  toggle.setAttribute("aria-expanded", String(state.navExpanded));
  document.getElementById("nav-toggle-label").textContent = state.navExpanded
    ? "Collapse navigation"
    : "Expand navigation";

  for (const item of document.querySelectorAll("[data-screen]")) {
    const selected = item.dataset.screen === state.screen;
    if (selected) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  for (const name of ["console", "settings"]) {
    document.getElementById(`screen-${name}`).hidden = name !== state.screen;
  }
}

function start() {
  document.getElementById("settings-host").textContent = window.location.host;
  document.getElementById("nav-toggle").addEventListener("click", () => {
    state.navExpanded = !state.navExpanded;
    render();
  });
  for (const item of document.querySelectorAll("[data-screen]")) {
    item.addEventListener("click", () => {
      state.screen = item.dataset.screen;
      render();
    });
  }
  render();
}

start();
