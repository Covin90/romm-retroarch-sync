// Modal + toast plumbing, shared by the ui and api shims.
//
// Lives in its own module because showModal/ModalRoot come from @decky/ui while
// toaster comes from @decky/api, and openFilePicker (api) needs to open a modal
// (ui) — routing both through here avoids a circular import.
import { useEffect, useState, type ReactNode } from "react";

// ── Modals ──────────────────────────────────────────────────────────────────

export type ModalHandle = { Close: () => void };

type ModalEntry = {
  id: number;
  render: (h: ModalHandle) => ReactNode;
  // The controller-focused element at open time, restored when this modal
  // closes so the pad returns to the opener (e.g. the platform row that
  // launched the core picker) instead of re-seeding onto the first focusable.
  opener: HTMLElement | null;
};

let modalSeq = 0;
const modals: ModalEntry[] = [];
const modalSubs = new Set<() => void>();
const notifyModals = () => modalSubs.forEach((f) => f());

// Restore focus to a modal's opener after React has committed the unmount.
// Guard on still-connected + not display:none; a plain .focus() re-triggers the
// gamepad marker mirror so the highlight follows.
function restoreOpener(el: HTMLElement | null) {
  if (!el) return;
  requestAnimationFrame(() => {
    if (!el.isConnected || (el.offsetWidth === 0 && el.offsetHeight === 0)) return;
    try { el.focus({ preventScroll: false } as any); } catch { /* gone */ }
  });
}

function popModal(id: number) {
  const i = modals.findIndex((m) => m.id === id);
  if (i === -1) return;
  const [entry] = modals.splice(i, 1);
  notifyModals();
  restoreOpener(entry.opener);
}

/**
 * Decky passes the modal element a `closeModal` prop. We clone that contract:
 * the caller supplies a render fn, we hand it a handle whose Close() pops it.
 */
export function pushModal(render: (h: ModalHandle) => ReactNode): ModalHandle {
  const id = ++modalSeq;
  const handle: ModalHandle = { Close: () => popModal(id) };
  const ae = document.activeElement as HTMLElement | null;
  modals.push({ id, render, opener: ae && ae !== document.body ? ae : null });
  notifyModals();
  return handle;
}

export function ModalHost() {
  const [, setRev] = useState(0);
  useEffect(() => {
    const bump = () => setRev((n) => n + 1);
    modalSubs.add(bump);
    return () => void modalSubs.delete(bump);
  }, []);

  if (!modals.length) return null;
  return (
    <>
      {modals.map((m) => {
        const handle: ModalHandle = { Close: () => popModal(m.id) };
        return (
          <div className="shim-modal-scrim" key={m.id}>
            {m.render(handle)}
          </div>
        );
      })}
    </>
  );
}

// ── Toasts ──────────────────────────────────────────────────────────────────

export type ToastOpts = {
  title?: ReactNode;
  body?: ReactNode;
  duration?: number;
  critical?: boolean;
  onClick?: () => void;
};

type ToastEntry = ToastOpts & { id: number };

let toastSeq = 0;
let toasts: ToastEntry[] = [];
const toastSubs = new Set<() => void>();
const notifyToasts = () => toastSubs.forEach((f) => f());

export function pushToast(opts: ToastOpts) {
  const id = ++toastSeq;
  toasts = [...toasts, { ...opts, id }];
  notifyToasts();
  const ms = opts.duration ?? 5000;
  window.setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    notifyToasts();
  }, ms);
}

// Notification-overlay corner, chosen in Settings (desktop-only). Persisted in
// localStorage so it survives restarts; the settings page dispatches
// `romm:toastpos` on change so the host repositions live without a reload.
const TOAST_POS_KEY = "romm:toastPos";
const TOAST_POSITIONS = ["top-right", "top-left", "bottom-right", "bottom-left"];
function readToastPos(): string {
  try {
    const v = localStorage.getItem(TOAST_POS_KEY);
    if (v && TOAST_POSITIONS.includes(v)) return v;
  } catch { /* private mode / no storage */ }
  return "top-right";
}

export function ToastHost() {
  const [, setRev] = useState(0);
  const [pos, setPos] = useState(readToastPos);
  useEffect(() => {
    const bump = () => setRev((n) => n + 1);
    toastSubs.add(bump);
    const onPos = () => setPos(readToastPos());
    window.addEventListener("romm:toastpos", onPos);
    return () => {
      toastSubs.delete(bump);
      window.removeEventListener("romm:toastpos", onPos);
    };
  }, []);

  return (
    <div className="shim-toast-host" data-pos={pos}>
      {toasts.map((t) => (
        <div
          key={t.id}
          className={"shim-toast" + (t.critical ? " shim-toast-critical" : "")}
          onClick={t.onClick}
        >
          {t.title ? <div className="shim-toast-title">{t.title}</div> : null}
          {t.body ? <div className="shim-toast-body">{t.body}</div> : null}
        </div>
      ))}
    </div>
  );
}
