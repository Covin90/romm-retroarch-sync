// Desktop button-hint bar — the equivalent of Steam's fixed bottom legend on
// the Deck. Steam renders that bar natively from each Focusable's
// on*ActionDescription / actionDescriptionMap props; our shim has no such chrome,
// so we rebuild it here. The gamepad layer (gamepad.ts) aggregates the current
// target's labels via computeLegend() and tells us when to refresh.
//
// Glyphs are drawn in-app (no external, trademarked console-button assets — and
// nothing to fetch under the app's strict CSP). The set is chosen from the
// connected controller's family: Xbox (colored ABXY), PlayStation (✕○□△),
// Switch (Nintendo-layout letters), or a neutral monochrome set otherwise.
import { useEffect, useState } from "react";
import {
  computeLegend,
  currentLegendTarget,
  onLegendChange,
  onControllerFamilyChange,
  controllerFamily,
  type ControllerFamily,
  type Legend,
} from "./gamepad";

// Per-family face-button appearance. Slots are the SEMANTIC actions (ok/cancel/
// secondary/options); each family maps them to the glyph the user physically
// sees. Switch swaps A/B and X/Y positions vs. the standard layout, so its
// letters differ from the button index.
type Face = { sym: string; color: string };
const FACE: Record<ControllerFamily, Record<string, Face>> = {
  neutral: {
    ok: { sym: "A", color: "#cfd2dc" },
    cancel: { sym: "B", color: "#cfd2dc" },
    secondary: { sym: "X", color: "#cfd2dc" },
    options: { sym: "Y", color: "#cfd2dc" },
  },
  xbox: {
    ok: { sym: "A", color: "#6cc04a" },
    cancel: { sym: "B", color: "#e74c3c" },
    secondary: { sym: "X", color: "#3b7ddd" },
    options: { sym: "Y", color: "#edb92e" },
  },
  ps: {
    ok: { sym: "✕", color: "#8a9be8" },       // ✕ cross
    cancel: { sym: "○", color: "#f0787f" },    // ○ circle
    secondary: { sym: "□", color: "#e58fc4" }, // □ square
    options: { sym: "△", color: "#67cbb0" },   // △ triangle
  },
  switch: {
    ok: { sym: "B", color: "#dfe2ea" },
    cancel: { sym: "A", color: "#dfe2ea" },
    secondary: { sym: "Y", color: "#dfe2ea" },
    options: { sym: "X", color: "#dfe2ea" },
  },
};

function FaceGlyph({ slot, family }: { slot: string; family: ControllerFamily }) {
  const f = FACE[family][slot];
  const colored = family === "xbox" || family === "ps";
  return (
    <span
      className="shim-legend-glyph"
      style={{
        borderColor: colored ? f.color : "rgba(255,255,255,0.35)",
        color: colored ? f.color : "#e6e8ee",
      }}
    >
      {f.sym}
    </span>
  );
}

// Select / Start are small pictographs (view = two overlapping panes, menu =
// three lines), kept family-neutral — the semantics read the same everywhere.
function SystemGlyph({ kind }: { kind: "select" | "start" }) {
  return (
    <span className="shim-legend-glyph shim-legend-glyph-sys" aria-hidden>
      {kind === "select" ? (
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
          <rect x="2.5" y="4.5" width="6.5" height="5" rx="1.2" stroke="currentColor" strokeWidth="1.3" />
          <rect x="7" y="6.5" width="6.5" height="5" rx="1.2" stroke="currentColor" strokeWidth="1.3" fill="#12121e" />
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
          <line x1="3.5" y1="5" x2="12.5" y2="5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          <line x1="3.5" y1="8" x2="12.5" y2="8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          <line x1="3.5" y1="11" x2="12.5" y2="11" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
      )}
    </span>
  );
}

function Hint({ children, label }: { children: any; label: string }) {
  return (
    <span className="shim-legend-hint">
      {children}
      <span className="shim-legend-label">{label}</span>
    </span>
  );
}

export function FooterLegend() {
  const [legend, setLegend] = useState<Legend>({});
  const [family, setFamily] = useState<ControllerFamily>(controllerFamily());

  useEffect(() => {
    let last = "";
    const recompute = () => {
      const l = computeLegend(currentLegendTarget());
      // Cheap identity check so we don't re-render on every mouse micro-move.
      const key = JSON.stringify(l);
      if (key !== last) { last = key; setLegend(l); }
    };
    recompute();
    const offL = onLegendChange(recompute);
    const offF = onControllerFamilyChange(() => setFamily(controllerFamily()));
    // Safety net: labels can change on the SAME focused element (e.g. a two-step
    // delete confirm) without any focus/hover event to trigger recompute.
    const iv = window.setInterval(recompute, 500);
    return () => { offL(); offF(); window.clearInterval(iv); };
  }, []);

  // Left cluster: system buttons. Right cluster: face-button actions.
  return (
    <div className="shim-legend">
      <div className="shim-legend-group">
        <Hint label="Navigate">
          <span className="shim-legend-glyph shim-legend-glyph-sys" aria-hidden>
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
              <path d="M8 2.2 L9.6 4 H6.4 Z M8 13.8 L6.4 12 H9.6 Z M2.2 8 L4 6.4 V9.6 Z M13.8 8 L12 9.6 V6.4 Z"
                fill="currentColor" />
            </svg>
          </span>
        </Hint>
        {legend.select && <Hint label={legend.select}><SystemGlyph kind="select" /></Hint>}
        {legend.start && <Hint label={legend.start}><SystemGlyph kind="start" /></Hint>}
      </div>
      <div className="shim-legend-group">
        {legend.ok && <Hint label={legend.ok}><FaceGlyph slot="ok" family={family} /></Hint>}
        {legend.secondary && <Hint label={legend.secondary}><FaceGlyph slot="secondary" family={family} /></Hint>}
        {legend.options && <Hint label={legend.options}><FaceGlyph slot="options" family={family} /></Hint>}
        {legend.cancel && <Hint label={legend.cancel}><FaceGlyph slot="cancel" family={family} /></Hint>}
      </div>
    </div>
  );
}
