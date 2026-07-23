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

// ── Action-description legend (the desktop equivalent of Steam's bottom
//    button-hint bar) ────────────────────────────────────────────────────────
//
// Every Focusable in index.tsx already declares what its buttons do via
// on*ActionDescription / actionDescriptionMap props. ui.tsx registers those here
// (as a live getter, so changing labels don't churn the map); the footer bar
// walks from the current target up the ancestor chain and shows, per button, the
// nearest ancestor that defines a label — exactly how Steam builds the Deck
// legend.

export type ActionDescs = {
  ok?: string;         // A / cross
  secondary?: string;  // X / square
  options?: string;    // Y / triangle
  activatable?: boolean; // has onActivate/onClick → A defaults to "Select"
  canCancel?: boolean;   // has onCancelButton → B shows "Back"
  select?: string;     // View / Select cluster
  start?: string;      // Menu / Start cluster
};

export type Legend = {
  ok?: string; cancel?: string; secondary?: string;
  options?: string; select?: string; start?: string;
};

const actionRegistry = new Map<HTMLElement, () => ActionDescs>();

export function registerActions(el: HTMLElement, get: () => ActionDescs) {
  actionRegistry.set(el, get);
  return () => actionRegistry.delete(el);
}

// Aggregate the legend for a target by walking up its Focusable ancestors,
// taking the first ancestor that supplies each slot.
export function computeLegend(target: HTMLElement | null): Legend {
  const out: Legend = {};
  let anyActivatable = false;
  let node: HTMLElement | null = target;
  while (node) {
    const get = actionRegistry.get(node);
    if (get) {
      const d = get();
      if (out.ok == null && d.ok) out.ok = d.ok;
      if (out.secondary == null && d.secondary) out.secondary = d.secondary;
      if (out.options == null && d.options) out.options = d.options;
      if (out.cancel == null && d.canCancel) out.cancel = "Back";
      if (out.select == null && d.select) out.select = d.select;
      if (out.start == null && d.start) out.start = d.start;
      if (d.activatable) anyActivatable = true;
    }
    node = node.parentElement;
  }
  if (out.ok == null && anyActivatable) out.ok = "Select";
  return out;
}

// Subscribers (the footer) are pinged whenever the current target may have
// changed — gamepad focus moves, mouse hover moves, focus lost.
const legendSubs = new Set<() => void>();
export function onLegendChange(cb: () => void) {
  legendSubs.add(cb);
  return () => legendSubs.delete(cb);
}
function notifyLegend() { legendSubs.forEach((f) => f()); }

// The element the legend should describe: whatever the pointer is over in mouse
// mode, else the gamepad-focused element.
export function currentLegendTarget(): HTMLElement | null {
  if (mouseMode) {
    return hovered && document.contains(hovered) ? hovered : null;
  }
  const a = document.activeElement as HTMLElement | null;
  return a && a !== document.body ? a : null;
}

// ── Controller family (drives which glyph set the footer draws) ──────────────
export type ControllerFamily = "xbox" | "ps" | "switch" | "neutral";
let _family: ControllerFamily = "neutral";
const familySubs = new Set<() => void>();
export function onControllerFamilyChange(cb: () => void) {
  familySubs.add(cb);
  return () => familySubs.delete(cb);
}
export function controllerFamily(): ControllerFamily { return _family; }

function detectFamily(id: string): ControllerFamily {
  const s = id.toLowerCase();
  // Vendor IDs are the most reliable signal; fall back to product-name keywords.
  if (/vendor:\s*054c|sony|dualshock|dualsense|playstation|\bps[345]\b/.test(s)) return "ps";
  if (/vendor:\s*045e|xbox|microsoft|xinput/.test(s)) return "xbox";
  if (/vendor:\s*057e|nintendo|switch|joy-con|joycon|pro controller/.test(s)) return "switch";
  return "neutral";
}

function refreshFamily() {
  let fam: ControllerFamily = "neutral";
  try {
    const pads = navigator.getGamepads ? navigator.getGamepads() : [];
    for (const p of pads) {
      if (p && p.connected) { fam = detectFamily(p.id || ""); break; }
    }
  } catch { /* no gamepad API */ }
  if (fam !== _family) { _family = fam; familySubs.forEach((f) => f()); }
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
  // "Rendered", not "on-screen". offsetParent===null catches display:none (so
  // the inactive, still-mounted home/platforms/collections panels are excluded),
  // and width/height>0 catches collapsed nodes. We deliberately do NOT require
  // the element to intersect the viewport: gamepad nav has no free-scroll wheel,
  // so a target still below the fold (a lower Home section) or off to the side
  // in a long horizontal row must stay reachable — move() navigates to it by
  // geometry and focusAndReveal() scrolls it into view. Gating on viewport
  // intersection is what dead-ended a Down move at the last on-screen row.
  if (el.offsetParent === null && getComputedStyle(el).position !== "fixed") {
    return false;
  }
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

// When a modal/context menu is open, trap focus inside the topmost one.
function focusRoot(): ParentNode {
  const overlays = document.querySelectorAll(".shim-modal, .shim-context-menu");
  return overlays.length ? overlays[overlays.length - 1] : document;
}

// Seed controller focus into an overlay's first target. On the Deck, ModalRoot
// hands gamepad focus to the modal it opens; the desktop shim has no Steam nav
// controller, so without this the pad's origin (document.activeElement) stays on
// the background control that opened the overlay — directional moves then compute
// from OUTSIDE the overlay and A-button routing walks the wrong subtree, leaving
// settings menus and start modals uncontrollable. Exported so ModalRoot/
// showContextMenu can seed on mount for an immediate highlight; move()/button()
// call the guard below as a net in case focus later escapes the overlay.
export function focusFirstIn(root: ParentNode): boolean {
  const all = (Array.from(root.querySelectorAll(FOCUS_SELECTOR)) as HTMLElement[])
    .filter(isVisible)
    // Don't land on the modal's ✕ close button — start on real content.
    .filter((el) => !el.classList.contains("shim-modal-close"));
  const inner = all.filter((el) => !all.some((o) => o !== el && el.contains(o)));
  const targets = dedupe(inner.map(fieldFootprint));
  const first = targets[0];
  if (!first) return false;
  // Leave focus alone if it's already on a real leaf target inside `root` — the
  // caller (a page/modal with its own autoFocus, or a re-assert tick) shouldn't
  // yank focus off a control the user is already on. Only act when focus sits on
  // nothing, the body, or a WRAPPER that merely encloses the targets (e.g. a
  // modal panel that grabbed autoFocus), which is the state that needs seeding.
  const active = document.activeElement as HTMLElement | null;
  if (active && active !== document.body && targets.includes(fieldFootprint(active))) {
    return true;
  }
  focusAndReveal(first, false, false);
  return true;
}

// Seed controller focus onto the current page/overlay's first target — used
// after a navigation so a gamepad user lands on a control by default. On the
// Deck, Steam's useAutoFocus does this; the shim honours useAutoFocus too, but
// pages without it (e.g. Settings) would otherwise come up with nothing
// selected. No-op in mouse mode (a mouse user needs no forced highlight), and
// focusFirstIn() leaves an already-focused control alone, so pages that DO
// self-focus (the library grid) are untouched.
export function seedFocus(): void {
  if (mouseMode) return;
  // If focus grabbed a WRAPPER that encloses targets (a page/panel with
  // autoFocus, e.g. SettingsPage's outer Focusable), seed within it so we land
  // on its first control rather than the global top nav that sits above it.
  const a = document.activeElement as HTMLElement | null;
  const root =
    a && a !== document.body && document.contains(a) && a.querySelector(FOCUS_SELECTOR)
      ? a
      : focusRoot();
  focusFirstIn(root);
}

// Whether focus currently sits on a real, visible, tabbable control (as opposed
// to <body>, a detached node, or a non-tabbable wrapper). Used to decide when a
// page/view swap has stranded the controller and needs a re-seed.
function focusIsUseful(): boolean {
  const a = document.activeElement as HTMLElement | null;
  if (!a || a === document.body || !document.contains(a)) return false;
  return a.matches(FOCUS_SELECTOR) && isVisible(a);
}

// If an overlay is open but focus escaped it, pull focus to the overlay's first
// target so both directional nav and button routing operate inside the overlay.
function ensureFocusInOverlay(): boolean {
  const root = focusRoot();
  if (root === document) return false;
  const active = document.activeElement as HTMLElement | null;
  if (active && (root as HTMLElement).contains(active)) return false;
  return focusFirstIn(root);
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

// Focus a target AND scroll it into view. On the Deck, Steam's nav controller
// scrolls the focused element into the viewport; a raw .focus() here only nudges
// the single nearest scrollable ancestor (and not at all for off-screen tiles in
// some WebKit cases), so gamepad moves would light up an element that stays
// clipped — no vertical scroll down a long grid, no horizontal scroll along a
// card row. scrollIntoView with block/inline 'nearest' walks EVERY scrollable
// ancestor, so both axes track the focus; it honours the page's
// scroll-padding-top (120px) so the item never hides under the sticky top bar.
// preventScroll on focus() stops the browser's own jump from fighting our scroll.
//
// `horizontal` (a Left/Right move) uses inline:'center' so the focused cover —
// scaled up with a glow that overflows its box — stays fully visible with room at
// a card row's far ends (inline:'nearest' would scroll it flush to the edge and
// clip the glow), giving the row a console-style "cursor centred, row slides under
// it" feel. A vertical move keeps inline:'nearest' instead: centring on Up/Down
// would also re-centre the LANDED row horizontally, so rows visibly slid sideways
// every time you moved between them — read as flicker/jitter. block:'nearest'
// keeps page scroll minimal and honours the 120px scroll-padding-top so nothing
// hides under the sticky top bar.
function focusAndReveal(el: HTMLElement, horizontal = false, smooth = true) {
  try { el.focus({ preventScroll: true } as any); }
  catch { el.focus(); }
  try {
    el.scrollIntoView({
      block: "nearest",
      inline: horizontal ? "center" : "nearest",
      behavior: smooth ? "smooth" : "auto",
    });
  } catch {
    try { el.scrollIntoView(); } catch { /* ignore */ }
  }
}

function center(el: HTMLElement) {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

// Pick the nearest focus target in `dir` from the currently focused element.
// Score favours alignment on the travel axis (cross-axis offset weighted
// heavier than same-axis distance), the usual spatial-nav heuristic.
// The content element focus left when an Up entered the top bar — restored when
// Down comes back out, so you return to the same cover.
let _preNavFocus: HTMLElement | null = null;

function move(dir: "up" | "down" | "left" | "right", smooth = true) {
  // Publish the travel axis so the plugin's letter-glimpse (index.tsx's
  // _tileFocusScrub, fired from the resulting onFocus) can tell a vertical grid
  // fly-through from ordinary horizontal row browsing — only the former should
  // raise the Big-Picture letter overlay.
  (window as any).__rommNavH = dir === "left" || dir === "right";

  const targets = focusTargets();
  if (!targets.length) return;

  // An overlay is open but focus never entered it (nothing seeded it, or focus
  // drifted back out): land on its first target rather than navigating from a
  // background control outside the overlay. The press that enters the overlay
  // just seeds the highlight, matching move()'s no-origin behaviour below.
  if (ensureFocusInOverlay()) return;

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
    focusAndReveal(targets[0], false, smooth);
    return;
  }
  // Focus sits on a WRAPPER that encloses the targets rather than on a target
  // itself — e.g. a modal panel with autoFocus (index.tsx focuses the inner
  // Focusable, not a leaf row). Every candidate is then a descendant of `active`
  // and gets skipped by the descendant guard in the scan below, dead-ending the
  // move so the overlay looks uncontrollable. Enter from the first target
  // instead, the same as the no-origin seed above.
  if (targets.every((t) => t !== active && active.contains(t))) {
    focusAndReveal(targets[0], false, smooth);
    return;
  }

  // Coming back DOWN out of the top bar returns to the cover you came UP from —
  // Steam remembers the bar's "preferred child" (the origin), and we mirror it so
  // Down doesn't dump you on the grid's first tile. Only when that cover is still
  // in the DOM and visible (same page); otherwise fall through to normal nav
  // (e.g. you switched tabs, so the old cover is gone/hidden).
  if (dir === "down" && inStickyTopBar(active) && _preNavFocus &&
      document.contains(_preNavFocus) && isVisible(_preNavFocus)) {
    focusAndReveal(_preNavFocus, false, smooth);
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
    // A horizontal move must stay within the focused element's row. Card rows
    // (Home) let you scroll off-screen tiles into view, so Left/Right needs to
    // reach same-row tiles that are currently clipped — but it must NEVER fall
    // back to a vertically-offset control in another row. Allowing that (the
    // penalty fallback) made a Left/Right at a row's edge jump to a neighbouring
    // row, which both swapped rows unpredictably and stranded you so the row
    // could never scroll back to its start. Requiring cross-overlap here keeps
    // off-screen same-row tiles eligible (they share the row's vertical extent)
    // while excluding other rows entirely. Vertical moves keep the fallback so
    // differently-x-aligned sections below the fold stay reachable.
    if (horizontal && penalty) continue;
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
    // Deprioritize the sticky top bar on an Up move so ANY real content row
    // above wins first — an Up must never leapfrog intervening rows straight
    // into the bar just because a tab is better column-aligned. Larger than the
    // misalignment penalty (1e6) so even an off-column row still beats the bar;
    // the bar is chosen only when nothing else sits above.
    const barPenalty = dir === "up" && inStickyTopBar(t) ? 1e9 : 0;
    const score = Math.abs(along) * 3 + Math.abs(cross) + penalty + barPenalty;
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

  // Entering the sticky top bar on an Up move, matched to the Deck in two steps.
  // First "reachable only at scroll top": the bar is pinned above the first
  // content row, so an Up from that row would reach it "before" the page has
  // scrolled up to show its top — so an Up that would enter the bar first scrolls
  // the page fully to the top; only once already there does a further Up hand
  // focus to the bar. Then, when focus does enter the bar, ALWAYS land inside the
  // nav tabs pill (the column-nearest tab) rather than the side clusters
  // (RetroDECK button / user pill) — on the Deck an Up into the nav always lands
  // on a nav tab. Skipped when the origin is itself in the bar (Left/Right along
  // the bar is untouched).
  // The trigger is: an Up move with no real content row above (best is null, or
  // spatial nav could only reach the bar). Note `best` can be null even from the
  // very first cover — the centered tabs sit up-and-to-the-side, so the
  // side-target guard rejects them; handling the null case is what makes Up from
  // the first cover work. When there IS a content row above, best is that row and
  // this is skipped (normal nav). Skipped too when the origin is already in the
  // bar (Left/Right along the bar is untouched).
  if (dir === "up" && !inStickyTopBar(active) && (!best || inStickyTopBar(best))) {
    const tab = navPillTarget(active);
    if (tab) {
      const sc = scrollParentOf(active);
      if (sc && sc.scrollTop > STICKY_TOP_EPS) {
        sc.scrollTo({ top: 0, behavior: smooth ? "smooth" : "auto" });
        return;
      }
      // Remember the cover we're leaving so a later Down returns to it.
      _preNavFocus = active;
      focusAndReveal(tab, false, smooth);
      return;
    }
  }

  if (best) focusAndReveal(best, horizontal, smooth);
}

// A sticky/fixed bar counts as "pinned to the top" when its box sits within this
// many px of the viewport top (the nav bar is ~58px tall).
const STICKY_TOP_MAX = 80;
// Treat the page as at-top within this slack so a hair of residual scroll still
// lets the bar take focus on the next Up.
const STICKY_TOP_EPS = 4;
// A top BAR is short; anything taller is a full-screen fixed layer (a modal /
// menu uses position:fixed inset:0) and must NOT count as the top bar — else
// every control inside a modal reads as "in the top bar" and the Up/Down nav
// guards fire against the background.
const STICKY_BAR_MAX_H = 160;

// True when `el` sits inside a position:sticky/fixed element pinned to the TOP of
// the page — the nav bar or a page header. These render above the first content
// row, so an Up from that row would otherwise jump straight into them. Excludes
// full-screen fixed overlays (modals) via the height cap.
function inStickyTopBar(el: HTMLElement): boolean {
  for (let node: HTMLElement | null = el; node; node = node.parentElement) {
    const pos = getComputedStyle(node).position;
    if (pos !== "sticky" && pos !== "fixed") continue;
    const r = node.getBoundingClientRect();
    if (r.top <= STICKY_TOP_MAX && r.height > 0 && r.height <= STICKY_BAR_MAX_H) {
      return true;
    }
  }
  return false;
}

// The nav tabs pill lives inside the sticky top bar, marked `.shim-topnav` by
// index.tsx. Return the FIRST focusable tab in the pill — an Up into the nav
// always lands there, regardless of which tile you came up from, matching the
// Deck. null if there's no pill.
function navPillTarget(inBar: HTMLElement): HTMLElement | null {
  let bar: HTMLElement | null = inBar;
  for (; bar; bar = bar.parentElement) {
    const pos = getComputedStyle(bar).position;
    if (pos === "sticky" || pos === "fixed") break;
  }
  const pill = (bar ?? document).querySelector(".shim-topnav") as HTMLElement | null;
  if (!pill) return null;
  const tabs = (Array.from(pill.querySelectorAll(FOCUS_SELECTOR)) as HTMLElement[])
    .filter(isVisible);
  if (!tabs.length) return null;
  // Land on the first tab that ISN'T the current page's (active) tab — matching
  // the Deck (on Home, Up lands on Platforms). Fall back to the first tab.
  return tabs.find((t) => !t.classList.contains("shim-navtab-active")) ?? tabs[0];
}

// Nearest vertically-scrollable ancestor of `el`, or null (the page scroll host).
function scrollParentOf(el: HTMLElement): HTMLElement | null {
  for (let node: HTMLElement | null = el.parentElement; node; node = node.parentElement) {
    const oy = getComputedStyle(node).overflowY;
    if ((oy === "auto" || oy === "scroll") && node.scrollHeight > node.clientHeight) {
      return node;
    }
  }
  return null;
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
// Two horizontal presses closer than this collapse to instant scroll (see
// direction()): fast L/R taps otherwise stack interrupted smooth-scroll layers
// and the focused cover shimmers/over-scales.
const H_COALESCE_MS = 140;

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

// Elements currently carrying Steam's gpfocus markers (see startGamepad). Kept as
// a list so a focus change removes exactly what the previous one added.
let _marked: HTMLElement[] = [];
function clearFocusMarkers() {
  for (const el of _marked) {
    el.classList.remove("gpfocus");
    el.classList.remove("gpfocuswithin");
  }
  _marked = [];
}
// Tag the focused leaf with `gpfocus` and it + every ancestor with
// `gpfocuswithin`, matching what Steam does on the Deck so the plugin's
// marker-based focus CSS lights up.
function markGamepadFocus(leaf: HTMLElement | null) {
  clearFocusMarkers();
  if (!leaf || leaf === document.body) return;
  leaf.classList.add("gpfocus");
  const marked: HTMLElement[] = [];
  for (let n: HTMLElement | null = leaf; n && n !== document.body; n = n.parentElement) {
    n.classList.add("gpfocuswithin");
    marked.push(n);
  }
  _marked = marked;
}
// The focus target currently under the mouse pointer (updated only on real
// pointer movement). When the controller takes over, gamepad nav seeds from this
// so mouse and pad are complementary: hover a cover with the mouse, then a D-pad
// press continues from THAT cover (and A activates it) instead of resuming from
// wherever focus happened to sit before.
let hovered: HTMLElement | null = null;
function enterGamepadMode() {
  // Continue from the hovered element even if we're already in gamepad mode:
  // moving the mouse (enterMouseMode) then pressing the pad must always hand off
  // from the pointer, and mouseMode may already be false from an earlier press.
  // Consume the hover hand-off exactly once: seed focus from it, then clear it so
  // subsequent presses navigate from the moved focus, not snap back to the cover
  // the pointer last sat on. A new pointer move re-arms it.
  if (hovered && document.contains(hovered) && isVisible(hovered)) {
    try { hovered.focus({ preventScroll: true } as any); }
    catch { hovered.focus(); }
  }
  hovered = null;
  if (!mouseMode) {
    // Already in gamepad mode (e.g. a repeat press): the focusin fired while
    // mouseMode was still true, or focus didn't change, so re-assert the marker.
    markGamepadFocus(document.activeElement as HTMLElement);
    return;
  }
  mouseMode = false;
  document.documentElement.style.cursor = "none";
  if (document.body) document.body.style.pointerEvents = "none";
  // The hovered.focus() above fired focusin while mouseMode was still true, so it
  // wasn't marked — mark the now-focused element for the gamepad highlight.
  markGamepadFocus(document.activeElement as HTMLElement);
}
function isEditable(el: HTMLElement) {
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
    el.isContentEditable;
}
let lastPointerX = NaN;
let lastPointerY = NaN;
function enterMouseMode(e?: Event) {
  // Scrolling content under a physically-stationary pointer makes WebKit
  // synthesize a mousemove (new element slides under the cursor). After a D-pad
  // press our smooth scrollIntoView does exactly that, so ~0.5s later a phantom
  // mousemove would hand control back to the mouse even though the user never
  // touched it. Real motion changes the pointer coordinates; a scroll-induced one
  // repeats the last position — ignore those so only genuine movement re-arms the
  // mouse. (Non-mousemove callers, e.g. an explicit switch, pass no event.)
  const me = e as MouseEvent | undefined;
  if (me && me.type === "mousemove") {
    if (me.clientX === lastPointerX && me.clientY === lastPointerY) return;
    lastPointerX = me.clientX;
    lastPointerY = me.clientY;
  }
  // Record what the pointer is over so a later gamepad press can resume from it.
  const t = e && (e.target as HTMLElement | null);
  const prevHovered = hovered;
  hovered = t ? (t.closest(FOCUS_SELECTOR) as HTMLElement | null) : hovered;
  // Update the footer legend as the pointer moves between focusable controls.
  if (hovered !== prevHovered) notifyLegend();
  if (mouseMode) return;
  mouseMode = true;
  document.documentElement.style.cursor = "";
  if (document.body) document.body.style.pointerEvents = "";
  // Clear the gamepad selection so its focus highlight (tile scale/glow/border,
  // driven by the Focusable's focus state) doesn't linger under the mouse's own
  // hover highlight — two selections at once. Blur the focused element on the
  // switch to mouse. Skip editable fields so moving the mouse mid-typing doesn't
  // kick the caret out of a text box.
  const active = document.activeElement as HTMLElement | null;
  if (active && active !== document.body && !isEditable(active)) active.blur();
  // Drop the gamepad focus markers so the pointer's own :hover highlight is the
  // only selection shown once the mouse takes over.
  clearFocusMarkers();
}

export function startGamepad() {
  window.addEventListener("mousemove", enterMouseMode, true);

  // Mirror Steam's gpfocus markers. On the Deck, Steam tags the gamepad-focused
  // Focusable AND its ancestors with `gpfocuswithin` (the leaf also with
  // `gpfocus`), and the plugin's focus CSS (.romm-row.gpfocuswithin,
  // .romm-*-wrap.gpfocuswithin, …) is built entirely on those markers. The shim
  // uses plain DOM focus, so without this rows/covers/tiles never highlight under
  // the controller. Only in gamepad mode: mouse hover has its own :hover rules,
  // and a lingering marker after the pointer takes over would double-highlight.
  window.addEventListener("focusin", (e) => {
    notifyLegend();
    if (mouseMode) return;
    markGamepadFocus(e.target as HTMLElement);
  }, true);
  // Focus leaving to nowhere (blur → body, no new target) clears the markers; a
  // real move fires a fresh focusin that re-marks.
  window.addEventListener("focusout", (e) => {
    if ((e as FocusEvent).relatedTarget == null) { clearFocusMarkers(); notifyLegend(); }
  }, true);

  // Keep the footer's glyph set in sync with the connected controller.
  refreshFamily();
  window.addEventListener("gamepadconnected", refreshFamily);
  window.addEventListener("gamepaddisconnected", () => { refreshFamily(); notifyLegend(); });

  // Re-seed focus when a page/view swap strands the controller. index.tsx swaps
  // the in-library views (Settings/Stats/Cores/Downloads) by toggling `display`
  // rather than navigating, which blurs the focused grid tile to <body>; those
  // views set no focus of their own (unlike the grid's useAutoFocus), so the pad
  // is left with nothing selected. Watch the tree and, once mutations settle,
  // seed the current page's first target — but ONLY when focus has actually
  // fallen to <body> in gamepad mode, so normal navigation (focus on a live
  // control) and mouse use are never disturbed.
  let reseedTimer: any = null;
  const observer = new MutationObserver(() => {
    if (mouseMode || focusIsUseful()) return;
    clearTimeout(reseedTimer);
    reseedTimer = setTimeout(() => {
      if (!mouseMode && !focusIsUseful()) seedFocus();
    }, 120);
  });
  if (document.body) observer.observe(document.body, { childList: true, subtree: true });

  let curDir: DirName | null = null;
  let delayTimer: any = null;
  let repeatTimer: any = null;
  // Timestamp of the last horizontal press, to coalesce fast dpad mashing.
  let lastHMove = 0;

  function direction(dir: DirName | null) {
    if (dir === curDir) return;
    if (dir) enterGamepadMode();
    curDir = dir;
    clearTimeout(delayTimer);
    clearInterval(repeatTimer);
    delayTimer = repeatTimer = null;
    if (dir) {
      // A single press animates (smooth); held-repeat steps jump instantly.
      // Smooth-scrolling every ~110ms repeat re-targets the in-flight animation
      // and the row oscillates, making covers shake during fast movement.
      //
      // Distinct FAST taps of Left/Right are the same hazard as held-repeat:
      // each new smooth scrollIntoView interrupts the previous one mid-flight,
      // so the card row's composited scroll layer never settles between presses.
      // At the ~1.8x page zoom the GPU keeps resampling that in-flight layer, and
      // the focused cover (scale(1.04) + glow) occasionally paints oversized/soft
      // for a frame — the intermittent "cover scales up big" shimmer. Coalesce:
      // if a horizontal press lands within COALESCE_MS of the previous one, jump
      // instantly (no animated layer to catch), keeping the smooth console-glide
      // only for relaxed single presses. Vertical moves are unaffected.
      const now = Date.now();
      const horizontal = dir === "left" || dir === "right";
      const smooth = !(horizontal && now - lastHMove < H_COALESCE_MS);
      if (horizontal) lastHMove = now;
      move(dir, smooth);
      delayTimer = setTimeout(() => {
        repeatTimer = setInterval(() => move(dir, false), REPEAT_INTERVAL);
      }, REPEAT_DELAY);
    }
  }

  function button(id: number, pressed: boolean) {
    if (pressed) { enterGamepadMode(); ensureFocusInOverlay(); }
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
