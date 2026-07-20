import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";

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
// full-window home: it carries its own nav (Settings/Stats/Cores/Downloads via
// its user menu) and self-forwards to the setup wizard when RomM isn't
// configured yet.
const HOME_ROUTE = "/romm-sync-library";

function App() {
  useRouteRegistry(); // re-render when routes are added or removed
  const path = useRoutePath();
  const route = matchRoute(path);

  // No matching route (initial "/" load, or a bare path) → go home. Never fall
  // back to the plugin panel.
  useEffect(() => {
    if (!route) Navigation.Navigate(HOME_ROUTE);
  }, [route]);

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
