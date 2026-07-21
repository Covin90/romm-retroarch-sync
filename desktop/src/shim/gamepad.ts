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
  const inner = all.filter((el) => !all.some((o) => o !== el && el.contains(o)));
  // Represent a wizard field by its full-width `.wiz-field` wrapper rather than
  // the inner <input>: the pair-code input is deliberately narrow-and-centered
  // (so its caret lands on the slots), which would otherwise give spatial nav a
  // tiny footprint that fails to line up with the off-centre footer buttons.
  // Using the wrapper's row-wide box makes vertical moves between fields and the
  // footer behave. Activation still works — OK clicks the wrapper Focusable,
  // whose onActivate focuses the inner input for typing.
  return dedupe(inner.map(fieldFootprint));
}

// The element spatial-nav should treat as `el`'s footprint: its enclosing
// wizard field wrapper if it sits in one, else the element itself.
function fieldFootprint(el: HTMLElement): HTMLElement {
  return (el.closest(".wiz-field") as HTMLElement | null) ?? el;
}

function dedupe(els: HTMLElement[]): HTMLElement[] {
  const seen = new Set<HTMLElement>();
  const out: HTMLElement[] = [];
  for (const el of els) if (!seen.has(el)) { seen.add(el); out.push(el); }
  return out;
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

  // Seed at the first target only when there's no usable origin at all. An
  // element can be focused yet absent from `targets`: focusTargets() collapses
  // nested focusables to the innermost, but the plugin's fields auto-focus the
  // OUTER wrapper (e.g. useAutoFocus on a Focusable). Teleporting to targets[0]
  // in that case sent the first D-pad press to the top of the step instead of
  // the neighbour below. Navigate from the focused element's own geometry
  // instead — its rect is the field's rect, so directional nav is correct.
  // Use the field footprint as the origin too, so a move that starts from a
  // narrow inner input (e.g. mid-typing in the pair-code field) still navigates
  // by the field's full row box.
  const rawActive = document.activeElement as HTMLElement | null;
  const active = rawActive ? fieldFootprint(rawActive) : null;
  if (!active || active === document.body) {
    targets[0].focus();
    return;
  }

  const from = center(active);
  const ar = active.getBoundingClientRect();
  const horizontal = dir === "left" || dir === "right";
  const sign = dir === "down" || dir === "right" ? 1 : -1;

  let best: HTMLElement | null = null;
  let bestScore = Infinity;
  for (const t of targets) {
    // Skip the origin and any target sharing its subtree (the field's own inner
    // input sits at the same spot — don't let it swallow the move).
    if (t === active || t.contains(active) || active.contains(t)) continue;
    const r = t.getBoundingClientRect();
    const c = center(t);
    const along = horizontal ? c.x - from.x : c.y - from.y;
    const cross = horizontal ? c.y - from.y : c.x - from.x;
    if (Math.sign(along) !== sign || along === 0) continue; // wrong direction
    // Strongly prefer targets whose cross-axis extent overlaps the active
    // element's: a Left/Right move should land on something in the same row,
    // not a control that's merely nearer diagonally (e.g. from the footer's
    // Next, Left must reach Back — not jump up to the centered field above it).
    // Misaligned targets are still eligible as a fallback (large penalty) so
    // navigation never dead-ends.
    const overlap = horizontal
      ? Math.min(ar.bottom, r.bottom) - Math.max(ar.top, r.top)
      : Math.min(ar.right, r.right) - Math.max(ar.left, r.left);
    const penalty = overlap > 0 ? 0 : 1e6;
    // Reject targets that are more to the SIDE than in the travel direction and
    // don't overlap the cross-axis: they're beside the origin, not ahead of it.
    // Without this, Up from the top ROM-directory field jumped to that row's
    // Browse button — rendered a few px higher (align-items:flex-end) with
    // nothing genuinely above — instead of doing nothing. Right still reaches
    // Browse (it cross-overlaps the field's row, so penalty is 0).
    if (penalty && Math.abs(along) < Math.abs(cross)) continue;
    // Weight the travel axis above the cross axis so the NEAREST row/column
    // wins among aligned candidates — not a control that's further along but
    // better column-aligned. On the Folders step the footer's Next sits in the
    // same column as the Browse buttons, so an Up from Next must reach the
    // Device-name field just above it, not leapfrog up the Browse column.
    const score = Math.abs(along) * 3 + Math.abs(cross) + penalty;
    if (score < bestScore) { bestScore = score; best = t; }
  }
  // Entering the wizard footer from above: land on the primary button directly
  // rather than on Back. index.tsx's footer auto-advances focus from Back to the
  // primary (a Deck focus-repair path), which on desktop shows as a Back→Next
  // flicker. Jump straight to the primary so there's no flash. Scoped to Down
  // (you only ever enter a footer by moving down onto it) and to a footer-shaped
  // container (a horizontal, space-between Focusable), so the top nav bar and
  // in-footer left/right moves are untouched.
  if (best && dir === "down") {
    const primary = footerPrimary(best, active);
    if (primary) best = primary;
  }

  if (best) best.focus();
}

// If `target` sits inside a footer-like row (horizontal Focusable using
// space-between) that the origin is NOT already inside, return that footer's
// last focus target (its primary button); otherwise null.
function footerPrimary(target: HTMLElement, origin: HTMLElement): HTMLElement | null {
  for (let node = target.parentElement; node; node = node.parentElement) {
    if (!node.classList.contains("shim-focusable")) continue;
    const cs = getComputedStyle(node);
    if (cs.display !== "flex" || cs.justifyContent !== "space-between" ||
        cs.flexDirection.startsWith("column")) continue;
    if (node.contains(origin)) return null; // already inside — don't hijack
    const inside = (Array.from(node.querySelectorAll(FOCUS_SELECTOR)) as HTMLElement[])
      .filter(isVisible);
    const last = inside[inside.length - 1];
    return last && last !== target ? last : null;
  }
  return null;
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

// Input-mode arbitration. On desktop the mouse pointer sits still over the
// window, so when the controller changes pages, elements render UNDER the
// stationary cursor and WebKit fires mouseenter — lighting up whatever the
// pointer happens to overlap (e.g. the Device-name field) with a hover
// highlight the gamepad never set and can't clear. While the controller is
// driving, disable pointer hit-testing (which suppresses hover/mouseenter but
// NOT the programmatic .focus()/.click() this layer uses) and hide the cursor;
// restore both the instant the real mouse moves.
let mouseMode = true;
function enterGamepadMode() {
  if (!mouseMode) return;
  mouseMode = false;
  document.documentElement.style.cursor = "none";
  if (document.body) document.body.style.pointerEvents = "none";
}
function enterMouseMode() {
  if (mouseMode) return;
  mouseMode = true;
  document.documentElement.style.cursor = "";
  if (document.body) document.body.style.pointerEvents = "";
}

export function startGamepad() {
  window.addEventListener("mousemove", enterMouseMode, true);

  let curDir: DirName | null = null;
  let delayTimer: any = null;
  let repeatTimer: any = null;

  function direction(dir: DirName | null) {
    if (dir === curDir) return;
    if (dir) enterGamepadMode();
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
    if (pressed) enterGamepadMode();
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
