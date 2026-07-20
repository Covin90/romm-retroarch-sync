// Gamepad → focus/navigation layer.
//
// On the Steam Deck, Steam reads the controller, moves focus between Focusable
// elements, and dispatches button events (onButtonDown/onCancelButton/…) into
// the focused subtree. The desktop app has none of that, so this module
// reproduces it against the W3C Gamepad API (WebKitGTK exposes it):
//
//   • d-pad / left stick  → move DOM focus to the nearest control in that
//     direction (spatial navigation).
//   • A (OK)              → onButtonDown(OK) on press; activate (click) on
//     release, so the tile tap/hold split still works.
//   • B / X / Y / bumpers / triggers / start / select → onButtonDown(<btn>)
//     routed up the Focusable ancestor chain, plus the dedicated
//     onCancelButton / onSecondaryButton / onOptionsButton callbacks.
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
  return (Array.from(root.querySelectorAll(FOCUS_SELECTOR)) as HTMLElement[])
    .filter(isVisible);
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
  else if (!active) targets[0].focus();
}

// ── Gamepad polling ─────────────────────────────────────────────────────────

// Standard-mapping button index → our GamepadButton enum value. Directions are
// handled as focus movement, not routed, so they map to a sentinel.
const DIR = -1;
const BUTTON_MAP: Record<number, number> = {
  0: GamepadButtonId.OK,             // A / South
  1: GamepadButtonId.CANCEL,         // B / East
  2: GamepadButtonId.SECONDARY,      // X / West
  3: GamepadButtonId.OPTIONS,        // Y / North
  4: GamepadButtonId.BUMPER_LEFT,    // LB
  5: GamepadButtonId.BUMPER_RIGHT,   // RB
  6: GamepadButtonId.TRIGGER_LEFT,   // LT
  7: GamepadButtonId.TRIGGER_RIGHT,  // RT
  8: GamepadButtonId.SELECT,         // Back / View
  9: GamepadButtonId.START,          // Start / Menu
  12: DIR, 13: DIR, 14: DIR, 15: DIR, // d-pad
};

const DEADZONE = 0.5;
const REPEAT_DELAY = 400;
const REPEAT_INTERVAL = 110;

type DirName = "up" | "down" | "left" | "right";

export function startGamepad() {
  const pressed = new Set<number>();          // button indices currently down
  let dir: DirName | null = null;             // active direction (dpad or stick)
  let dirNextRepeat = 0;

  const dedicatedFor = (btn: number): keyof FocusHandlers | undefined => {
    if (btn === GamepadButtonId.CANCEL) return "onCancelButton";
    if (btn === GamepadButtonId.SECONDARY) return "onSecondaryButton";
    if (btn === GamepadButtonId.OPTIONS) return "onOptionsButton";
    if (btn === GamepadButtonId.START) return "onMenuButton";
    return undefined;
  };

  function readDirection(gp: Gamepad): DirName | null {
    const b = gp.buttons;
    if (b[12]?.pressed) return "up";
    if (b[13]?.pressed) return "down";
    if (b[14]?.pressed) return "left";
    if (b[15]?.pressed) return "right";
    const [ax, ay] = gp.axes;
    if (ay <= -DEADZONE) return "up";
    if (ay >= DEADZONE) return "down";
    if (ax <= -DEADZONE) return "left";
    if (ax >= DEADZONE) return "right";
    return null;
  }

  function tick() {
    const pads = navigator.getGamepads?.() ?? [];
    const gp = Array.from(pads).find((p) => p) as Gamepad | undefined;
    if (gp) {
      const now = performance.now();

      // Directions: initial press + auto-repeat while held.
      const d = readDirection(gp);
      if (d && d !== dir) {
        dir = d; dirNextRepeat = now + REPEAT_DELAY; move(d);
      } else if (d && d === dir && now >= dirNextRepeat) {
        dirNextRepeat = now + REPEAT_INTERVAL; move(d);
      } else if (!d) {
        dir = null;
      }

      // Buttons: edge-detected press/release.
      gp.buttons.forEach((btn, i) => {
        const mapped = BUTTON_MAP[i];
        if (mapped === undefined || mapped === DIR) return;
        if (btn.pressed && !pressed.has(i)) {
          pressed.add(i);
          if (mapped === GamepadButtonId.OK) {
            routeButton("down", mapped, false);
          } else {
            routeButton("down", mapped, false, dedicatedFor(mapped));
          }
        } else if (!btn.pressed && pressed.has(i)) {
          pressed.delete(i);
          routeButton("up", mapped, false);
          if (mapped === GamepadButtonId.OK) {
            // Tap-activate on release (tile onActivate; native button click).
            (document.activeElement as HTMLElement | null)?.click();
          }
        }
      });
    }
    requestAnimationFrame(tick);
  }

  requestAnimationFrame(tick);
}
