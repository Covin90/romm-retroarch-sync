import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ModalHost, ToastHost } from "./shim/overlays";
import { matchRoute, useRoutePath, useRouteRegistry } from "./shim/router";
import "./shim/shim.css";

// Importing the plugin runs its definePlugin factory, which registers every
// route. Must happen before first render so the router already has them.
//
// NOTE: this is decky_plugin/src/index.tsx, imported unmodified. Vite aliases
// @decky/ui and @decky/api to the shim (see vite.config.ts).
import plugin from "../../decky_plugin/src/index";

function App() {
  useRouteRegistry(); // re-render when routes are added or removed
  const path = useRoutePath();
  const route = matchRoute(path);

  // The plugin's sidebar panel is its entry surface; on desktop it becomes the
  // home page, with the registered routes as the pages it navigates to.
  // NOTE: definePlugin's `content` is a React ELEMENT (`<Content />`), not a
  // component — render it as a node, never `<Content/>` (that throws #130).
  return (
    <div className="shim-app">
      {route ? route.component() : (plugin?.content ?? null)}
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
