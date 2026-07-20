// Route registry + imperative navigation.
//
// Deliberately hand-rolled rather than react-router: Navigation.* is called
// from module scope (outside any component), which routers make awkward, and
// the plugin's route params are decorative — pages like LibraryGamesPage take
// no props and read module-level state instead. So all that's actually needed
// is "match a path, render a component".
import { useEffect, useState } from "react";

type RouteEntry = {
  path: string;
  component: () => JSX.Element;
  exact?: boolean;
};

const routes = new Map<string, RouteEntry>();
const routeSubs = new Set<() => void>();
const pathSubs = new Set<() => void>();

const notifyRoutes = () => routeSubs.forEach((f) => f());
const notifyPath = () => pathSubs.forEach((f) => f());

export const routerHook = {
  addRoute(
    path: string,
    component: () => JSX.Element,
    opts?: { exact?: boolean },
  ) {
    routes.set(path, { path, component, exact: opts?.exact });
    notifyRoutes();
  },
  removeRoute(path: string) {
    routes.delete(path);
    notifyRoutes();
  },
};

export function navigate(to: string) {
  if (to === window.location.pathname) return;
  window.history.pushState({}, "", to);
  notifyPath();
}

export const Navigation = {
  Navigate: (path: string) => navigate(path),
  NavigateBack: () => window.history.back(),
  NavigateToExternalWeb: (url: string) =>
    window.open(url, "_blank", "noopener,noreferrer"),
  // No side menus on desktop — the plugin calls this defensively before
  // navigating, so a no-op is correct rather than merely tolerable.
  CloseSideMenus: () => {},
};

// The plugin only ever reaches Steam internals through optional chaining
// (`(Router as any)?.WindowStore?.…`), so an empty object degrades to
// undefined at every call site instead of throwing.
export const Router = {};

/** `/a/:id` matches `/a/42`. Trailing slashes are insignificant. */
function pathMatches(pattern: string, actual: string): boolean {
  const p = pattern.replace(/\/+$/, "").split("/");
  const a = actual.replace(/\/+$/, "").split("/");
  if (p.length !== a.length) return false;
  return p.every((seg, i) => seg.startsWith(":") || seg === a[i]);
}

/** Most specific wins: fewer params beats more. */
export function matchRoute(path: string): RouteEntry | undefined {
  const hits = [...routes.values()].filter((r) => pathMatches(r.path, path));
  if (hits.length <= 1) return hits[0];
  const params = (r: RouteEntry) =>
    r.path.split("/").filter((s) => s.startsWith(":")).length;
  return hits.sort((x, y) => params(x) - params(y))[0];
}

export function useRoutePath(): string {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onChange = () => setPath(window.location.pathname);
    pathSubs.add(onChange);
    window.addEventListener("popstate", onChange);
    return () => {
      pathSubs.delete(onChange);
      window.removeEventListener("popstate", onChange);
    };
  }, []);
  return path;
}

/** Re-renders when addRoute/removeRoute fires, so late routes appear. */
export function useRouteRegistry(): number {
  const [rev, setRev] = useState(0);
  useEffect(() => {
    const bump = () => setRev((n) => n + 1);
    routeSubs.add(bump);
    return () => void routeSubs.delete(bump);
  }, []);
  return rev;
}
