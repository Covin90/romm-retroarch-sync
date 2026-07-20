import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";

import { callable } from "./shim/api";
import { ModalHost, ToastHost } from "./shim/overlays";
import { Navigation, matchRoute, useRoutePath, useRouteRegistry } from "./shim/router";
import "./shim/shim.css";

// Importing the plugin runs its definePlugin factory, which registers every
// route. Must happen before first render so the router already has them. This
// is a side-effect import — we deliberately do NOT use the returned plugin
// object (see below).
//
// NOTE: this is decky_plugin/src/index.tsx, imported unmodified. Vite aliases
// @decky/ui and @decky/api to the shim (see vite.config.ts).
import "../../decky_plugin/src/index";

// Where the desktop app lands. The Decky Quick Access panel (plugin.content) is
// a Deck-only surface — sized for the 240px sidebar and reached via SteamOS —
// so the desktop app never renders it. The library browser is the real
// full-window home; the setup wizard is where an unconfigured install begins.
const LIBRARY_ROUTE = "/romm-sync-library";
const SETUP_ROUTE = "/romm-sync-setup";

const getConfig = callable<[], { configured?: boolean }>("get_config");

function App() {
  useRouteRegistry(); // re-render when routes are added or removed
  const path = useRoutePath();
  const route = matchRoute(path);

  // No matching route (initial "/" load, or a bare path) → pick the landing
  // page from config and go there directly. We must NOT trampoline through the
  // library route while unconfigured: LibraryRootPage bounces an unconfigured
  // install back with NavigateBack(), which lands on "/" again → this effect
  // re-fires → flicker loop between library and setup. Resolving the
  // destination here (setup when unconfigured, library otherwise) means the
  // library page only ever mounts when it will actually stay.
  useEffect(() => {
    if (route) return;
    let cancelled = false;
    (async () => {
      let dest = LIBRARY_ROUTE;
      try {
        const c = await getConfig();
        if (!c?.configured) dest = SETUP_ROUTE;
      } catch {
        dest = SETUP_ROUTE; // backend unreachable → send to setup, not a blank
      }
      if (!cancelled) Navigation.Navigate(dest);
    })();
    return () => { cancelled = true; };
  }, [route, path]);

  return (
    <div className="shim-app">
      {route ? route.component() : null}
      <ModalHost />
      <ToastHost />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
