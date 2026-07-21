// Gamepad → focus/navigation layer.
//
// On the Steam Deck, Steam reads the controller, moves focus between Focusable
// elements, and dispatches button events (onButtonDown/onCancelButton/…) into
// the focused subtree. The desktop app reproduces that here.
//
// Input does NOT come from the W3C Gamepad API: WebKitGTK's mapping for
// Xbox-compatible pads over Bluetooth is broken (d-pad and triggers collapse
// onto stick axes, indistinguishable). Instead the native shell reads the
// controller with libmanette (correct, named events) and calls the injection
// API this module installs on `window.__rommGamepad`:
//
//   • direction("up"|"down"|"left"|"right"|null) → spatial focus move + repeat
//   • button(id, pressed) → onButtonDown/onButtonUp routed up the Focusable
//     tree; A(OK) also activates (clicks) the focused control on release.
//
// Focusable (ui.tsx) registers its button handlers here so we can route to the
// focused subtree; focus *targets* are found by DOM query so native buttons and
// inputs participate too.

import { GamepadButtonId } from "./gamepad-buttons";

// ── Focusable registry (button-event routing) ───────────────────────────────

export type FocusHandlers = {
  onButtonDown?: (e: any) => void;
  onButtonUp?: (e: any) => void;
  onCancelButton?: (e: any) => void;
  onSecondaryButton?: (e: any) => void;
  onOptionsButton?: (e: any) => void;
  onMenuButton?: (e: any) => void;
};

const registry = new Map<HTMLElement, FocusHandlers>();

export function registerFocusable(el: HTMLElement, handlers: FocusHandlers) {
  registry.set(el, handlers);
  return () => registry.delete(el);
}

function synthEvent(button: number, isRepeat: boolean) {
  let stopped = false;
  return {
    detail: { button, is_repeat: isRepeat },
    stopPropagation() { stopped = true; },
    preventDefault() {},
    get _stopped() { return stopped; },
  };
}

// Walk from the focused element up the DOM, invoking each registered ancestor's
// onButtonDown (Steam lets a parent Focusable observe every press in its tree).
// The dedicated callback (onCancelButton/…) fires on the nearest ancestor that
// defines it. Stops if a handler calls stopPropagation().
function routeButton(
  kind: "down" | "up",
  button: number,
  isRepeat: boolean,
  dedicated?: keyof FocusHandlers,
) {
  const start = (document.activeElement as HTMLElement) ?? document.body;
  const e = synthEvent(button, isRepeat);
  let dedicatedFired = false;
  let node: HTMLElement | null = start;
  while (node) {
    const h = registry.get(node);
    if (h) {
      if (kind === "down") h.onButtonDown?.(e);
      else h.onButtonUp?.(e);
      if (dedicated && !dedicatedFired && h[dedicated]) {
        (h[dedicated] as (e: any) => void)(e);
        dedicatedFired = true;
      }
      if ((e as any)._stopped) break;
    }
    node = node.parentElement;
  }
}

// ── Focus targets (spatial navigation) ──────────────────────────────────────

const FOCUS_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function isVisible(el: HTMLElement) {
  if (el.offsetParent === null && getComputedStyle(el).position !== "fixed") {
    return false;
  }
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 &&
    r.bottom > 0 && r.right > 0 &&
    r.top < window.innerHeight && r.left < window.innerWidth;
}

// When a modal/context menu is open, trap focus inside the topmost one.
function focusRoot(): ParentNode {
  const overlays = document.querySelectorAll(".shim-modal, .shim-context-menu");
  return overlays.length ? overlays[overlays.length - 1] : document;
}

function focusTargets(): HTMLElement[] {
  const root = focusRoot();
  const all = (Array.from(root.querySelectorAll(FOCUS_SELECTOR)) as HTMLElement[])
    .filter(isVisible);
  // Collapse nested focusables to a single stop. The plugin wraps controls in a
  // Focusable (which the shim makes tabbable), so a text field is BOTH the
  // wrapper div and the inner <input> — two landing spots for one field, which
  // is what made a field take two presses (and drop into edit mode on the
  // second). Keep only the innermost interactive element: if a candidate
  // contains another candidate, drop the outer one. Button routing still works
  // because routeButton() walks DOM ancestors from the focused element up.
  return all.filter((el) => !all.some((o) => o !== el && el.contains(o)));
}

function center(el: HTMLElement) {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

// Pick the nearest focus target in `dir` from the currently focused element.
// Score favours alignment on the travel axis (cross-axis offset weighted
// heavier than same-axis distance), the usual spatial-nav heuristic.
function move(dir: "up" | "down" | "left" | "right") {
  const targets = focusTargets();
  if (!targets.length) return;

  const active = document.activeElement as HTMLElement | null;
  if (!active || !targets.includes(active)) {
    targets[0].focus();
    return;
  }

  const from = center(active);
  const horizontal = dir === "left" || dir === "right";
  const sign = dir === "down" || dir === "right" ? 1 : -1;

  let best: HTMLElement | null = null;
  let bestScore = Infinity;
  for (const t of targets) {
    if (t === active) continue;
    const c = center(t);
    const along = horizontal ? c.x - from.x : c.y - from.y;
    const cross = horizontal ? c.y - from.y : c.x - from.x;
    if (Math.sign(along) !== sign || along === 0) continue; // wrong direction
    const score = Math.abs(along) + Math.abs(cross) * 2;
    if (score < bestScore) { bestScore = score; best = t; }
  }
  if (best) best.focus();
}

// ── Injection API (driven by the native libmanette bridge) ───────────────────

const REPEAT_DELAY = 400;
const REPEAT_INTERVAL = 110;

type DirName = "up" | "down" | "left" | "right";

function dedicatedFor(btn: number): keyof FocusHandlers | undefined {
  if (btn === GamepadButtonId.CANCEL) return "onCancelButton";
  if (btn === GamepadButtonId.SECONDARY) return "onSecondaryButton";
  if (btn === GamepadButtonId.OPTIONS) return "onOptionsButton";
  if (btn === GamepadButtonId.START) return "onMenuButton";
  return undefined;
}

declare global {
  interface Window {
    __rommGamepad?: {
      direction: (dir: DirName | null) => void;
      button: (id: number, pressed: boolean) => void;
    };
  }
}

export function startGamepad() {
  let curDir: DirName | null = null;
  let delayTimer: any = null;
  let repeatTimer: any = null;

  function direction(dir: DirName | null) {
    if (dir === curDir) return;
    curDir = dir;
    clearTimeout(delayTimer);
    clearInterval(repeatTimer);
    delayTimer = repeatTimer = null;
    if (dir) {
      move(dir);
      delayTimer = setTimeout(() => {
        repeatTimer = setInterval(() => move(dir), REPEAT_INTERVAL);
      }, REPEAT_DELAY);
    }
  }

  function button(id: number, pressed: boolean) {
    if (pressed) {
      routeButton("down", id, false,
        id === GamepadButtonId.OK ? undefined : dedicatedFor(id));
    } else {
      routeButton("up", id, false);
      if (id === GamepadButtonId.OK) {
        // Tap-activate on release (tile onActivate; native button click).
        (document.activeElement as HTMLElement | null)?.click();
      }
    }
  }

  window.__rommGamepad = { direction, button };
}
