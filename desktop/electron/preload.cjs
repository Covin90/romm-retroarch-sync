// Renderer-side gamepad bridge for the Electron shell.
//
// The GTK shell reads the controller natively with libmanette because
// WebKitGTK's W3C Gamepad API mangles Xbox-compatible pads over Bluetooth (d-pad
// and triggers collapse onto stick axes). Chromium's Gamepad API does NOT have
// that bug, so here we poll navigator.getGamepads() directly and drive the same
// `window.__rommGamepad` API that src/shim/gamepad.ts installs — no native
// module. If Chromium's mapping ever proves unreliable on some pad, this is the
// single place to swap in a node-hid/XInput fallback.
//
// Mapping cross-check — Chromium "standard" layout → GamepadButtonId, verified
// against the libmanette table in gamepad_bridge.py's docstring:
//
//   standard btn  physical   GamepadButtonId          libmanette code
//   ───────────── ────────── ──────────────────────── ───────────────
//   0             A          OK (1)                    304
//   1             B          CANCEL (2)                305
//   2             X          SECONDARY (3)             307
//   3             Y          OPTIONS (4)               308
//   4             LB         BUMPER_LEFT (9)           310
//   5             RB         BUMPER_RIGHT (10)         311
//   6             LT         TRIGGER_LEFT (7)          axis 10
//   7             RT         TRIGGER_RIGHT (8)         axis 9
//   8             Back/View  SELECT (6)                314
//   9             Start/Menu START (5)                 315
//   12/13/14/15   d-pad U/D/L/R  → direction()         hat 17/16
//   axes 0/1      left stick → direction() fallback    axis 0/1
//
// Triggers are analog buttons in the standard layout, so we threshold .value the
// same way the bridge thresholds the L2/R2 axis. Direction favours the vertical
// axis first, exactly like gamepad_bridge._emit_dir.

const { ipcRenderer } = require("electron");

// Desktop-only bridge the shared plugin UI feature-detects (window.__rommDesktop)
// to show an Exit row in the account menu — the Deck build has no such object.
window.__rommDesktop = {
  quit() { ipcRenderer.send("romm:quit"); },
};

const OK = 1, CANCEL = 2, SECONDARY = 3, OPTIONS = 4;
const START = 5, SELECT = 6, TRIGGER_LEFT = 7, TRIGGER_RIGHT = 8;
const BUMPER_LEFT = 9, BUMPER_RIGHT = 10;

// standard-mapping button index → GamepadButtonId (d-pad handled separately).
const BUTTON_MAP = {
  0: OK, 1: CANCEL, 2: SECONDARY, 3: OPTIONS,
  4: BUMPER_LEFT, 5: BUMPER_RIGHT,
  6: TRIGGER_LEFT, 7: TRIGGER_RIGHT,
  8: SELECT, 9: START,
};

const DPAD = { up: 12, down: 13, left: 14, right: 15 };
const TRIGGER_ON = 0.3;    // analog trigger past this counts as pressed
const STICK_DEADZONE = 0.6; // matches gamepad_bridge STICK_DEADZONE

function pressed(btn) {
  if (!btn) return false;
  return typeof btn === "object"
    ? btn.pressed || btn.value > TRIGGER_ON
    : btn > TRIGGER_ON;
}

function startPolling() {
  const btnState = {};        // GamepadButtonId → last-sent pressed bool
  let dirState = null;        // last-sent direction string | null

  function emitButton(id, isDown) {
    if (btnState[id] === isDown) return;
    const firstSighting = !(id in btnState);
    btnState[id] = isDown;
    // Don't announce a release for a button we never saw pressed — the frame a
    // pad first appears would otherwise blast button(id,false) for every idle
    // control, spuriously firing onButtonUp handlers.
    if (firstSighting && !isDown) return;
    window.__rommGamepad && window.__rommGamepad.button(id, isDown);
  }
  function emitDir(dir) {
    if (dir === dirState) return;
    dirState = dir;
    window.__rommGamepad && window.__rommGamepad.direction(dir);
  }

  function releaseAll() {
    for (const id of Object.keys(btnState)) emitButton(Number(id), false);
    emitDir(null);
  }

  function poll() {
    // Don't drive the app while it's in the background — when a game launches,
    // RetroArch opens its own window and takes focus, but our renderer keeps
    // polling navigator.getGamepads() and would otherwise move the hidden UI
    // under the same stick/button presses meant for the game. Gate on real
    // window focus: release any held inputs and idle until the app is focused
    // again (alt-tab back, or the emulator exits).
    if (!document.hasFocus()) {
      releaseAll();
      requestAnimationFrame(poll);
      return;
    }

    const pads = navigator.getGamepads ? navigator.getGamepads() : [];
    let gp = null;
    for (const p of pads) { if (p && p.connected) { gp = p; break; } }

    if (!gp) {
      releaseAll();
      requestAnimationFrame(poll);
      return;
    }

    // Face/shoulder/trigger/menu buttons.
    for (const [idx, id] of Object.entries(BUTTON_MAP)) {
      emitButton(id, pressed(gp.buttons[idx]));
    }

    // Direction: d-pad buttons first, else the left stick past the deadzone.
    // Vertical wins over horizontal (mirrors gamepad_bridge._emit_dir).
    const axX = gp.axes[0] || 0, axY = gp.axes[1] || 0;
    const up = pressed(gp.buttons[DPAD.up]) || axY < -STICK_DEADZONE;
    const down = pressed(gp.buttons[DPAD.down]) || axY > STICK_DEADZONE;
    const left = pressed(gp.buttons[DPAD.left]) || axX < -STICK_DEADZONE;
    const right = pressed(gp.buttons[DPAD.right]) || axX > STICK_DEADZONE;
    const dir = up ? "up" : down ? "down" : left ? "left" : right ? "right" : null;
    emitDir(dir);

    requestAnimationFrame(poll);
  }

  // Chromium only starts exposing a HOT-PLUGGED pad through getGamepads() once
  // the page has a gamepadconnected listener registered — polling alone sees
  // pads present at load, but not ones turned on later. Subscribing arms that
  // device monitoring; the handlers just reset our cached state so a reconnect
  // starts clean (the poll loop below does the actual reading every frame).
  window.addEventListener("gamepadconnected", () => { /* poll picks it up */ });
  window.addEventListener("gamepaddisconnected", releaseAll);

  requestAnimationFrame(poll);
}

// The app installs window.__rommGamepad inside startGamepad(); our poller guards
// each call, so we can begin polling as soon as the DOM is ready.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", startPolling, { once: true });
} else {
  startPolling();
}
