"""Controller input for the desktop window, via libmanette.

WebKitGTK's W3C Gamepad API mangles Xbox-compatible pads over Bluetooth (d-pad
and triggers collapse onto stick axes), so we read the controller natively with
libmanette instead — it reports clean, separated events — and inject them into
the page through the `window.__rommGamepad` API that shim/gamepad.ts installs.

libmanette event layout observed on an Xbox-compatible pad:
  • buttons: A=304, B=305, X=307, Y=308, LB=310, RB=311, Select=314, Start=315
  • d-pad:   hat index 17 = vertical (up -1 / down +1),
             hat index 16 = horizontal (left -1 / right +1)
  • triggers: absolute-axis index 10 = L2, index 9 = R2 (rest -1 → +1 pressed)
  • left stick: absolute-axis index 0 = X, index 1 = Y (used for navigation too)

Manette events fire on the GLib main loop (the GTK thread), so the inject
callback can touch the WebView directly.
"""
import gi

gi.require_version("Manette", "0.2")
from gi.repository import Manette  # noqa: E402

# GamepadButton enum values — must match shim/gamepad-buttons.ts.
_OK, _CANCEL, _SECONDARY, _OPTIONS = 1, 2, 3, 4
_START, _SELECT = 5, 6
_TRIGGER_LEFT, _TRIGGER_RIGHT = 7, 8
_BUMPER_LEFT, _BUMPER_RIGHT = 9, 10

# libmanette button hardware codes → GamepadButton id.
BUTTON_MAP = {
    304: _OK, 305: _CANCEL, 307: _SECONDARY, 308: _OPTIONS,
    310: _BUMPER_LEFT, 311: _BUMPER_RIGHT, 314: _SELECT, 315: _START,
}
# absolute-axis index → trigger GamepadButton id.
TRIGGER_AXIS = {10: _TRIGGER_LEFT, 9: _TRIGGER_RIGHT}
TRIGGER_ON = -0.3      # triggers rest at -1; treat past this as "pressed"
STICK_DEADZONE = 0.6

HAT_Y, HAT_X = 17, 16
STICK_X, STICK_Y = 0, 1


class GamepadBridge:
    def __init__(self, inject):
        """inject: callable(js: str) invoked on the GTK main thread."""
        self._inject = inject
        self._hat_x = 0
        self._hat_y = 0
        self._sx = 0.0
        self._sy = 0.0
        self._trig = {_TRIGGER_LEFT: False, _TRIGGER_RIGHT: False}
        self._dir = None
        self._wired = set()  # device ids already connected (avoid double events)

        self._monitor = Manette.Monitor.new()
        try:
            self._monitor.connect("device-connected", self._on_connected)
        except Exception:
            pass
        it = self._monitor.iterate()
        while True:
            res = it.next()
            ok = res[0] if isinstance(res, tuple) else bool(res)
            dev = res[1] if isinstance(res, tuple) and len(res) > 1 else None
            if not ok:
                break
            if dev is not None:
                self._add(dev)

    # ── device wiring ────────────────────────────────────────────────────────

    def _on_connected(self, _monitor, device):
        self._add(device)

    def _add(self, dev):
        if id(dev) in self._wired:
            return
        self._wired.add(id(dev))
        for sig, cb in (
            ("button-press-event", self._press),
            ("button-release-event", self._release),
            ("absolute-axis-event", self._axis),
            ("hat-axis-event", self._hat),
        ):
            try:
                dev.connect(sig, cb)
            except Exception:
                pass  # some devices lack a given event class

    # ── injection helpers ────────────────────────────────────────────────────

    def _button(self, gid, pressed):
        self._inject(
            f"window.__rommGamepad&&window.__rommGamepad.button"
            f"({gid},{'true' if pressed else 'false'})"
        )

    def _emit_dir(self):
        y = self._hat_y or (-1 if self._sy < -STICK_DEADZONE
                            else 1 if self._sy > STICK_DEADZONE else 0)
        x = self._hat_x or (-1 if self._sx < -STICK_DEADZONE
                            else 1 if self._sx > STICK_DEADZONE else 0)
        d = ("up" if y < 0 else "down" if y > 0
             else "left" if x < 0 else "right" if x > 0 else None)
        if d != self._dir:
            self._dir = d
            arg = f"'{d}'" if d else "null"
            self._inject(
                f"window.__rommGamepad&&window.__rommGamepad.direction({arg})"
            )

    # ── event handlers ───────────────────────────────────────────────────────

    def _press(self, _dev, ev):
        ok, code = ev.get_button()
        gid = BUTTON_MAP.get(code)
        if gid:
            self._button(gid, True)

    def _release(self, _dev, ev):
        ok, code = ev.get_button()
        gid = BUTTON_MAP.get(code)
        if gid:
            self._button(gid, False)

    def _hat(self, _dev, ev):
        r = ev.get_hat()  # (ok, index, value)
        idx, val = r[1], r[2]
        if idx == HAT_Y:
            self._hat_y = val
        elif idx == HAT_X:
            self._hat_x = val
        self._emit_dir()

    def _axis(self, _dev, ev):
        r = ev.get_absolute()  # (ok, index, value)
        idx, val = r[1], r[2]
        if idx in TRIGGER_AXIS:
            gid = TRIGGER_AXIS[idx]
            pressed = val > TRIGGER_ON
            if pressed != self._trig[gid]:
                self._trig[gid] = pressed
                self._button(gid, pressed)
        # Left-stick navigation is intentionally NOT wired: the stick axis
        # indices aren't confirmed on this pad, and an axis that rests off-centre
        # would emit a held direction on its own ("phantom" navigation). The
        # d-pad (hat) covers directional input. Revisit once stick axes are
        # verified.
