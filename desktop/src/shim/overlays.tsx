// Modal + toast plumbing, shared by the ui and api shims.
//
// Lives in its own module because showModal/ModalRoot come from @decky/ui while
// toaster comes from @decky/api, and openFilePicker (api) needs to open a modal
// (ui) — routing both through here avoids a circular import.
import { useEffect, useState, type ReactNode } from "react";

// ── Modals ──────────────────────────────────────────────────────────────────

export type ModalHandle = { Close: () => void };

type ModalEntry = { id: number; render: (h: ModalHandle) => ReactNode };

let modalSeq = 0;
const modals: ModalEntry[] = [];
const modalSubs = new Set<() => void>();
const notifyModals = () => modalSubs.forEach((f) => f());

/**
 * Decky passes the modal element a `closeModal` prop. We clone that contract:
 * the caller supplies a render fn, we hand it a handle whose Close() pops it.
 */
export function pushModal(render: (h: ModalHandle) => ReactNode): ModalHandle {
  const id = ++modalSeq;
  const handle: ModalHandle = {
    Close: () => {
      const i = modals.findIndex((m) => m.id === id);
      if (i !== -1) {
        modals.splice(i, 1);
        notifyModals();
      }
    },
  };
  modals.push({ id, render });
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
        const handle: ModalHandle = {
          Close: () => {
            const i = modals.findIndex((x) => x.id === m.id);
            if (i !== -1) {
              modals.splice(i, 1);
              notifyModals();
            }
          },
        };
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

export function ToastHost() {
  const [, setRev] = useState(0);
  useEffect(() => {
    const bump = () => setRev((n) => n + 1);
    toastSubs.add(bump);
    return () => void toastSubs.delete(bump);
  }, []);

  return (
    <div className="shim-toast-host">
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
