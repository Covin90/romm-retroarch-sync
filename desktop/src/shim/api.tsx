// Web implementations of the @decky/api surface consumed by index.tsx.
import { useState } from "react";
import { pushToast, pushModal, type ToastOpts } from "./overlays";

export { routerHook } from "./router";

// ── RPC ─────────────────────────────────────────────────────────────────────

const RPC_BASE = "/api";

/**
 * Decky's `callable` binds a name to a Python method on the plugin backend and
 * marshals positional args. The desktop backend mirrors that contract, so the
 * 51 call sites in index.tsx need no changes: POST /api/<method> with
 * {args: [...]}, reply {result} or {error}.
 */
export function callable<A extends any[], R>(method: string) {
  return async (...args: A): Promise<R> => {
    const res = await fetch(`${RPC_BASE}/${encodeURIComponent(method)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args }),
    });
    if (!res.ok) {
      throw new Error(`${method}: HTTP ${res.status} ${res.statusText}`);
    }
    const payload = await res.json();
    if (payload && payload.error) throw new Error(String(payload.error));
    return payload.result as R;
  };
}

// ── Plugin entry ────────────────────────────────────────────────────────────

export type PluginDescriptor = {
  name?: string;
  title?: any;
  content?: any;
  icon?: any;
  onDismount?: () => void;
  alwaysRender?: boolean;
};

/**
 * Decky calls the factory once at load and keeps the descriptor. Same here —
 * running it at import time is what registers the plugin's routes, so the
 * router has them before first render.
 */
export function definePlugin(factory: () => PluginDescriptor): PluginDescriptor {
  return factory();
}

// ── Toasts ──────────────────────────────────────────────────────────────────

export const toaster = {
  toast: (opts: ToastOpts) => pushToast(opts),
};

// ── File picker ─────────────────────────────────────────────────────────────

export enum FileSelectionType {
  FILE = 0,
  FOLDER = 1,
}

export type FilePickerRes = { path: string; realpath: string };

type DirListing = {
  path: string;
  parent: string | null;
  entries: { name: string; path: string; isdir: boolean }[];
};

// The only shim entry that needs real backend support: a browser cannot
// enumerate the filesystem, and <input type=file webkitdirectory> yields file
// lists rather than a directory path. So the backend exposes a listing endpoint
// and this renders a picker over it.
const listDir = callable<[string, boolean], DirListing>("shim_list_dir");

function FolderPicker({
  startPath,
  includeFiles,
  onPick,
  closeModal,
}: {
  startPath: string;
  includeFiles: boolean;
  onPick: (res: FilePickerRes | null) => void;
  closeModal?: () => void;
}) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cwd, setCwd] = useState(startPath);

  const load = async (path: string) => {
    try {
      setError(null);
      const res = await listDir(path, includeFiles);
      setListing(res);
      setCwd(res.path);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
  };

  if (!listing && !error) void load(startPath);

  const done = (res: FilePickerRes | null) => {
    onPick(res);
    closeModal?.();
  };

  return (
    <div className="shim-modal shim-picker">
      <div className="shim-picker-path">{cwd}</div>
      {error ? <div className="shim-picker-error">{error}</div> : null}
      <div className="shim-picker-list">
        {listing?.parent ? (
          <button
            className="shim-picker-entry"
            type="button"
            onClick={() => void load(listing.parent!)}
          >
            📁 ..
          </button>
        ) : null}
        {listing?.entries.map((e) => (
          <button
            key={e.path}
            className="shim-picker-entry"
            type="button"
            onClick={() =>
              e.isdir
                ? void load(e.path)
                : done({ path: e.path, realpath: e.path })
            }
          >
            {e.isdir ? "📁" : "📄"} {e.name}
          </button>
        ))}
      </div>
      <div className="shim-picker-actions">
        <button type="button" onClick={() => done(null)}>
          Cancel
        </button>
        <button
          type="button"
          onClick={() => done({ path: cwd, realpath: cwd })}
        >
          Select this folder
        </button>
      </div>
    </div>
  );
}

/**
 * Mirrors Decky's signature. Resolves with {path, realpath}; rejects on cancel,
 * which is what the call sites expect (they wrap it in try/catch).
 */
export function openFilePicker(
  select: FileSelectionType,
  startPath: string,
  includeFiles: boolean = false,
  _includeFolders: boolean = true,
): Promise<FilePickerRes> {
  return new Promise((resolve, reject) => {
    pushModal((handle) => (
      <FolderPicker
        startPath={startPath || "/"}
        includeFiles={includeFiles || select === FileSelectionType.FILE}
        closeModal={handle.Close}
        onPick={(res) => (res ? resolve(res) : reject(new Error("cancelled")))}
      />
    ));
  });
}
