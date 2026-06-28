import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  TextField,
  Navigation,
  staticClasses,
  DialogButton,
  Focusable,
  GamepadButton,
  showModal,
  ModalRoot,
  showContextMenu,
  Menu,
  MenuItem,
} from "@decky/ui";
import { callable, definePlugin, toaster, routerHook, openFilePicker, FileSelectionType } from "@decky/api";
import { useState, useEffect, useLayoutEffect, useRef, forwardRef, type Ref, type ChangeEvent } from "react";
import { FaSync, FaTrash, FaCog, FaGithub, FaBug, FaUndo, FaCopy, FaGamepad, FaBookmark, FaHome, FaSearch, FaTimes, FaDownload, FaPlay, FaInfoCircle, FaRegClock, FaLayerGroup, FaChevronLeft, FaChevronRight, FaCheckCircle, FaUsers, FaExternalLinkAlt, FaPuzzlePiece, FaBoxOpen, FaClone, FaRedo, FaClock, FaCheck, FaEllipsisH, FaGlobe } from "react-icons/fa";
import { BsGearFill } from "react-icons/bs";
import { MdVerified } from "react-icons/md";

// Call backend methods
const getServiceStatus = callable<[], any>("get_service_status");
const refreshFromRomm = callable<[boolean], any>("refresh_from_romm");
const getLoggingEnabled = callable<[], boolean>("get_logging_enabled");
const updateLoggingEnabled = callable<[boolean], boolean>("set_logging_enabled");
const getConfig = callable<[], any>("get_config");
const logout = callable<[boolean], any>("logout");
const getAccountUsername = callable<[], any>("get_account_username");
const saveConfig = callable<[string, string, string, string, string, string, string], any>("save_config");
const testRommConnection = callable<[string, string, string], any>("test_connection");
const pairDevice = callable<[string, string], any>("pair_device");
const getSaveHistory = callable<[number], any>("get_save_history");
const getSaveScreenshot = callable<[number, number, string], any>("get_save_screenshot");
const restoreSaveVersion = callable<[number, number, string, boolean], any>("restore_save_version");
// Game Browser
const getLibraryGroups = callable<[string], any>("get_library_groups");
const getLibraryGames = callable<[string, string], any>("get_library_games");
const getGameCover = callable<[number, boolean], any>("get_game_cover");
const getImage = callable<[string], any>("get_image");
const clearCoverCache = callable<[], any>("clear_cover_cache");
const searchGames = callable<[string], any>("search_games");
const getGameDetail = callable<[number], any>("get_game_detail");
const downloadGame = callable<[number], any>("download_game");
const toggleCollectionSync = callable<[string, boolean], any>("toggle_collection_sync");
const deleteCollectionRoms = callable<[string], any>("delete_collection_roms");
const getDownloadProgress = callable<[number], any>("get_download_progress");
const deleteGame = callable<[number], any>("delete_game");
const launchGame = callable<[number, (string | null)?, (number | null)?], any>("launch_game");
// Steam Deck session-host launch: resolves argv + writes a launch-spec; the tile
// is then RunGame'd so the emulator is a child of a Steam-tracked game (overlay).
const prepareSteamLaunch = callable<[number, (string | null)?, (number | null)?], any>("prepare_steam_launch");
const getSessionHostPath = callable<[], any>("get_session_host_path");
const getLocalDiscs = callable<[number], any>("get_local_discs");
const getLocalSiblings = callable<[number], any>("get_local_siblings");
const getHomeData = callable<[], any>("get_home_data");
const getPluginLogo = callable<[], any>("get_plugin_logo");
const getRommArtwork = callable<[], any>("get_romm_artwork");
const getRommLogo = callable<[], any>("get_romm_logo");

// Shared image-fetch queue. The home page mounts dozens of tiles at once, each
// of which needs a base64 cover / screenshot / platform-icon over RPC. Firing
// them all in parallel slams the backend (it serves one image at a time) and
// stalls the whole grid. Instead we funnel every image RPC through a small
// concurrency-limited queue so tiles fill in progressively, FIFO (≈ left to
// right / top to bottom) — the same one-at-a-time backbone the Save Data page
// uses for its state screenshots.
function makeImageQueue(concurrency: number) {
  let active = 0;
  const pending: Array<() => void> = [];
  const pump = () => {
    while (active < concurrency && pending.length) {
      active++;
      pending.shift()!();
    }
  };
  return function enqueue<T>(job: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      pending.push(() => {
        job().then(resolve, reject).finally(() => { active--; pump(); });
      });
      pump();
    });
  };
}
const imageQueue = makeImageQueue(3);
const qGetImage = (path: string) => imageQueue(() => getImage(path));
const qGetGameCover = (romId: number, large: boolean) => imageQueue(() => getGameCover(romId, large));

// Decoded-art cache (data URIs) so covers persist across tile remounts and can be
// prefetched for neighbouring groups — the grid then slides in fully painted
// instead of popping each cover in as its base64 fetch lands.
// Keys: `cover:${romId}:${large}` for ROM covers, `img:${path}` for screenshots.
const _coverCache = new Map<string, string | null>();   // resolved results only
const _coverInflight = new Map<string, Promise<string | null>>(); // dedup in-flight
// Sync peek: resolved URI (string | null) or undefined if not yet loaded.
const peekCover = (key: string): string | null | undefined =>
  _coverCache.has(key) ? _coverCache.get(key) : undefined;
// Deduped fetch into the cache; safe to call from many tiles / prefetch at once.
function awaitCover(key: string, fetcher: () => Promise<{ data_uri?: string } | null>): Promise<string | null> {
  if (_coverCache.has(key)) return Promise.resolve(_coverCache.get(key)!);
  let p = _coverInflight.get(key);
  if (!p) {
    p = fetcher()
      .then((r) => { const u = r?.data_uri || null; _coverCache.set(key, u); _coverInflight.delete(key); return u; })
      .catch(() => { _coverInflight.delete(key); return null; });
    _coverInflight.set(key, p);
  }
  return p;
}

// ---------------------------------------------------------------------------
// RomM v2 visual language (from rommapp/romm frontend/src/v2/styles/tokens.css).
// The Game Browser renders custom divs styled with these tokens instead of the
// Steam/Decky chrome; Focusable is used only for gamepad navigation.
// ---------------------------------------------------------------------------
const V2 = {
  bg: '#07070f',
  surface: 'rgba(255,255,255,0.07)',
  surfaceHover: 'rgba(255,255,255,0.12)',
  border: 'rgba(255,255,255,0.07)',
  borderStrong: 'rgba(255,255,255,0.15)',
  fg: '#ffffff',
  fg2: 'rgba(255,255,255,0.75)',
  fgMuted: 'rgba(255,255,255,0.45)',
  fgFaint: 'rgba(255,255,255,0.25)',
  bgElevated: 'rgba(255,255,255,0.045)',
  brand: '#8b74e8',
  brandHover: '#a18fff',
  brandPressed: '#6043c8',
  success: '#4ade80',
  warning: '#fbbf24',
  danger: '#ff5050',
  igdb: '#6366f1',
  ra: '#ef4444',
  coverPlaceholder: '#1a1a2e',
  radiusArt: '8px',
  radiusSm: '4px',
  radiusMd: '8px',
  radiusChip: '6px',
  radiusLg: '10px',
  radiusCard: '14px',
  radiusPill: '100px',
  elev2: '0 8px 24px rgba(0,0,0,.45)',
  font: '"Motiva Sans","Segoe UI",system-ui,-apple-system,sans-serif',
};

// RomM GameActionBtn round buttons: glassy scrim with blur (default), or the
// "emphasized" white look used by Play. Circular; size in px.
function roundBtn(size: number, variant: 'glass' | 'emphasized' | 'danger'): any {
  const base: any = {
    width: `${size}px`, height: `${size}px`, borderRadius: '50%',
    display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
    backdropFilter: 'blur(6px)', WebkitBackdropFilter: 'blur(6px)',
    transition: 'background 0.15s, color 0.15s, border-color 0.15s',
  };
  if (variant === 'emphasized') return { ...base, background: '#ffffff', border: '1px solid #ffffff', color: '#111117' };
  if (variant === 'danger') return { ...base, background: 'rgba(0,0,0,0.55)', border: '1px solid rgba(255,80,80,0.55)', color: V2.danger };
  return { ...base, background: 'rgba(0,0,0,0.55)', border: '1px solid rgba(255,255,255,0.12)', color: 'rgba(255,255,255,0.95)' };
}

interface LibGroup {
  key: string; label: string; count: number; downloaded: number | null;
  kind?: 'favorite' | 'smart' | 'virtual' | 'collection'; covers?: string[];
  slug?: string | null; fs_slug?: string | null;
  synced?: boolean; virtual?: boolean;
}
interface LibGame { rom_id: number; name: string; platform: string | null; is_downloaded: boolean; has_cover: boolean; screenshot?: string | null; platform_slug?: string | null; is_multi_disc?: boolean; disc_count?: number; sibling_roms?: { rom_id: number; name: string }[]; region_count?: number; }

function fmtBytes(n: number | null | undefined): string {
  if (!n || isNaN(n as any)) return '';
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB']) {
    if (v < 1024) return u === 'B' ? `${v.toFixed(0)} ${u}` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v.toFixed(1)} TB`;
}

// Full release date "02 Jan 2024" — RomM GameHeader meta uses the localized
// day/short-month/year form rather than just the year.
function fmtReleaseDate(ts: number | null | undefined): string {
  if (!ts) return '';
  try {
    const n = Number(ts);
    if (n > 3000) return String(n);
    const d = new Date(n * 1000);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' });
  } catch { return ''; }
}

// Blurred full-bleed background art — the defining RomM v2 surface. The
// focused/first cover is painted behind everything, heavily blurred and dimmed,
// with a bg-coloured gradient scrim on top (recipe lifted verbatim from RomM's
// frontend/src/v2/styles/global.css: blur(28px) brightness(0.45), scale 1.08).
function V2Bg({ uri }: { uri: string | null }) {
  // RomM's BackgroundArt falls back to /assets/auth_background.svg when no cover
  // is set (platform/collection index pages). Fetch it once as the default.
  const [fallback, setFallback] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    (async () => {
      try { const r = await getImage('/assets/auth_background.svg'); if (alive) setFallback(r?.data_uri || null); }
      catch { /* ignore */ }
    })();
    return () => { alive = false; };
  }, []);
  const shown = uri || fallback;
  return (
    <>
      <div style={{
        position: 'fixed', inset: 0, zIndex: 0, pointerEvents: 'none',
        backgroundImage: shown ? `url('${shown}')` : 'none',
        backgroundColor: V2.bg,
        backgroundSize: 'cover', backgroundPosition: 'center 20%', backgroundRepeat: 'no-repeat',
        filter: 'blur(28px) brightness(0.45)', transform: 'scale(1.08)',
        transition: 'background-image 0.5s ease',
      }} />
      <div style={{
        position: 'fixed', inset: 0, zIndex: 1, pointerEvents: 'none',
        background:
          `linear-gradient(to right, rgba(7,7,15,0.72) 0%, rgba(7,7,15,0.30) 55%, rgba(7,7,15,0.55) 100%),` +
          `linear-gradient(to bottom, rgba(7,7,15,0.10) 0%, rgba(7,7,15,0) 35%, rgba(7,7,15,0.70) 72%, rgba(7,7,15,0.92) 100%)`,
      }} />
    </>
  );
}

// Lazy, cached cover art. Renders a placeholder until the base64 data URI
// arrives from the backend (the frontend <img> can't auth to RomM directly).
// `onLoaded` bubbles the URI up so a tile can feed the blurred background art.
function GameCover({ romId, hasCover, large = false, radius = V2.radiusArt, onLoaded }:
  { romId: number; hasCover: boolean; large?: boolean; radius?: string; onLoaded?: (uri: string | null) => void }) {
  const ck = `cover:${romId}:${large}`;
  const peek = peekCover(ck);
  const [uri, setUri] = useState<string | null>(peek ?? null);
  const [done, setDone] = useState(peek !== undefined);
  useEffect(() => {
    let alive = true;
    if (!hasCover) { setDone(true); onLoaded?.(null); return; }
    const p = peekCover(ck);
    if (p !== undefined) { setUri(p); setDone(true); onLoaded?.(p); return; }
    (async () => {
      const u = await awaitCover(ck, () => qGetGameCover(romId, large));
      if (alive) { setUri(u); onLoaded?.(u); setDone(true); }
    })();
    return () => { alive = false; };
  }, [romId, large]);
  return (
    <div style={{
      position: 'relative', width: '100%', aspectRatio: '3 / 4',
      background: V2.coverPlaceholder, borderRadius: radius, overflow: 'hidden',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      {uri ? (
        <img src={uri} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
      ) : (
        <span style={{ color: V2.fgMuted, fontSize: '11px' }}>{done ? 'No cover' : '…'}</span>
      )}
    </div>
  );
}

// Landscape screenshot art (RomM continue-playing cover override). Fetches the
// screenshot path as base64 via get_image, fills the art box object-fit:cover.
// `onLoaded` bubbles the URI so the card can feed it to the background art.
function ScreenshotArt({ path, onLoaded, onRatio }:
  { path: string; onLoaded?: (uri: string | null) => void; onRatio?: (ratio: number) => void }) {
  const ik = `img:${path}`;
  const [uri, setUri] = useState<string | null>(peekCover(ik) ?? null);
  useEffect(() => {
    let alive = true;
    (async () => {
      const u = await awaitCover(ik, () => qGetImage(path));
      if (alive) { setUri(u); onLoaded?.(u); }
    })();
    return () => { alive = false; };
  }, [path]);
  return (
    <div style={{
      position: 'absolute', inset: 0, background: V2.coverPlaceholder,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      {uri && <img src={uri}
        onLoad={(e: any) => {
          const w = e.target?.naturalWidth, h = e.target?.naturalHeight;
          if (w && h) onRatio?.(w / h);
        }}
        style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />}
    </div>
  );
}

// Cover-art PIP — a small 2D box-art thumbnail floated bottom-right while a
// screenshot covers the rom's own art, so the game stays identifiable (RomM
// CoverArtPip). Fades out on focus so it never collides with the action row.
function CoverPip({ romId, hasCover, hidden }:
  { romId: number; hasCover: boolean; hidden: boolean }) {
  const [uri, setUri] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    if (!hasCover) return;
    (async () => {
      try { const r = await qGetGameCover(romId, false); if (alive) setUri(r?.data_uri || null); }
      catch { /* ignore */ }
    })();
    return () => { alive = false; };
  }, [romId]);
  if (!uri) return null;
  return (
    <div style={{
      position: 'absolute', right: '6px', bottom: '6px', width: '46px',
      aspectRatio: '3 / 4', zIndex: 2, borderRadius: V2.radiusSm, overflow: 'hidden',
      border: '1.5px solid rgba(255,255,255,0.12)',
      boxShadow: '0 2px 8px rgba(0,0,0,0.45)', pointerEvents: 'none',
      opacity: hidden ? 0 : 1, transition: 'opacity 0.12s ease',
    }}>
      <img src={uri} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
    </div>
  );
}

// A single library grid card: art-only with a centered, single-line label
// underneath (RomM's GameCard language). Focus/hover scales the art and paints
// the brand glow; activating it opens the game; gaining focus feeds the cover
// to the page's background art. When `game.screenshot` is set (continue-playing
// rail), the art is a landscape screenshot with the box-art floated as a PIP,
// matching RomM's Home — otherwise it's the portrait cover.
// Polls get_download_progress until the background download reaches a terminal
// state, resolving with the outcome. The download itself is kicked off by
// download_game (which returns immediately); this drives completion + the toast.
async function awaitDownload(romId: number): Promise<{ ok: boolean; message?: string }> {
  for (; ;) {
    let p: any;
    try { p = await getDownloadProgress(romId); }
    catch { await new Promise((r) => setTimeout(r, 500)); continue; }
    if (p?.state === 'done') return { ok: true };
    if (p?.state === 'error') return { ok: false, message: p.message };
    if (p?.state === 'idle') return { ok: false, message: 'Download did not start' };
    await new Promise((r) => setTimeout(r, 400));
  }
}

// Global registry of in-flight downloads (by rom_id) so EVERY surface showing a
// game (its cover tile + its details page) reflects the download — not just the
// one whose button was clicked. doDownload toggles membership; surfaces subscribe.
const _dlActive = new Set<number>();
const _dlListeners = new Set<() => void>();
function _setDlActive(romId: number, on: boolean) {
  if (on) _dlActive.add(romId); else _dlActive.delete(romId);
  _dlListeners.forEach((l) => { try { l(); } catch { } });
}
// Downloads one game through the same path as the tile button: registers it in
// the global registry (so its cover tile shows the ring), kicks off the backend
// download, then polls to completion. Returns whether it succeeded.
async function downloadOne(romId: number): Promise<boolean> {
  if (_dlActive.has(romId)) { // already downloading elsewhere — just wait it out
    return (await awaitDownload(romId)).ok;
  }
  _setDlActive(romId, true);
  try {
    const start = await downloadGame(romId);
    if (!start?.success) return false;
    return (await awaitDownload(romId)).ok;
  } catch { return false; }
  finally { _setDlActive(romId, false); }
}

// Runs a batch of downloads with bounded concurrency, reporting progress after
// each one finishes. Returns the count that succeeded.
async function downloadBatch(
  romIds: number[], concurrency: number, onProgress: (done: number, ok: number) => void,
): Promise<number> {
  let done = 0, ok = 0, i = 0;
  const worker = async () => {
    while (i < romIds.length) {
      const id = romIds[i++];
      if (await downloadOne(id)) ok++;
      done++;
      onProgress(done, ok);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, romIds.length) }, worker));
  return ok;
}

function useIsDownloading(romId: number): boolean {
  const [v, setV] = useState(_dlActive.has(romId));
  useEffect(() => {
    const l = () => setV(_dlActive.has(romId));
    _dlListeners.add(l); l();
    return () => { _dlListeners.delete(l); };
  }, [romId]);
  return v;
}

// Polls the backend for a game's live download progress (0..100) while a download
// is in flight, returning null when idle. Drives the cover download-ring and the
// GameDetails button fill. Activates on either the local `active` flag or the
// global registry, so progress is shared across the cover tile and details page.
interface DlProgress { percent: number; speed: number; eta: number; }
function useDownloadProgress(romId: number, active: boolean): DlProgress | null {
  const globalActive = useIsDownloading(romId);
  const on = active || globalActive;
  const [prog, setProg] = useState<DlProgress | null>(null);
  useEffect(() => {
    if (!on) { setProg(null); return; }
    let cancelled = false;
    const tick = async () => {
      try {
        const p = await getDownloadProgress(romId);
        if (!cancelled && p && typeof p.percent === 'number') {
          setProg({ percent: p.percent, speed: p.speed || 0, eta: p.eta || 0 });
        }
      } catch { /* transient */ }
    };
    tick();
    const id = setInterval(tick, 400);
    return () => { cancelled = true; clearInterval(id); };
  }, [romId, on]);
  return prog;
}

// Eases a displayed integer toward a target so a stepwise percentage (updated
// every poll) counts up smoothly. Resets to 0 when inactive.
function useSmoothNumber(target: number | null, active: boolean): number {
  const [val, setVal] = useState(0);
  const cur = useRef(0);
  const raf = useRef<any>(null);
  useEffect(() => {
    if (!active) { cur.current = 0; setVal(0); return; }
    const t = target ?? 0;
    const step = () => {
      const diff = t - cur.current;
      if (Math.abs(diff) < 0.5) { cur.current = t; setVal(t); return; }
      cur.current += diff * 0.18;
      setVal(cur.current);
      raf.current = requestAnimationFrame(step);
    };
    cancelAnimationFrame(raf.current);
    raf.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf.current);
  }, [target, active]);
  return Math.round(val);
}

// Estimates remaining seconds from the velocity of a 0..100 percentage: anchors
// at the start of a run and divides the remaining percent by the average rate.
// Generic enough to drive a collection ETA whether the bytes come from the
// one-shot batch or the background sync worker. Returns 0 until it has a rate.
function useEtaFromPct(pct: number, active: boolean): number {
  const anchor = useRef<{ t: number; p: number } | null>(null);
  const [eta, setEta] = useState(0);
  useEffect(() => {
    if (!active) { anchor.current = null; setEta(0); return; }
    const now = Date.now();
    // (Re)anchor at run start or if progress resets (a new run).
    if (!anchor.current || pct < anchor.current.p) anchor.current = { t: now, p: pct };
    const dt = (now - anchor.current.t) / 1000;
    const dp = pct - anchor.current.p;
    if (dt > 2 && dp > 0.5) setEta(Math.max(0, ((100 - pct) / (dp / dt))));
  }, [pct, active]);
  return eta;
}

// "1m 23s" / "45s" — compact ETA from a seconds count (0 / unknown → '').
const formatEta = (secs: number): string => {
  if (!secs || secs <= 0 || !isFinite(secs)) return '';
  const s = Math.round(secs);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
};

// Circular progress ring drawn around a centered glyph (used on the cover's
// download button). `pct` null renders an empty track.
function ProgressRing({ pct, size = 40, stroke = 3, glow = false, children }:
  { pct: number | null; size?: number; stroke?: number; glow?: boolean; children?: any }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const p = Math.max(0, Math.min(100, pct ?? 0));
  return (
    <div style={{ position: 'relative', width: size, height: size, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <svg width={size} height={size} style={{ position: 'absolute', top: 0, left: 0, transform: 'rotate(-90deg)', overflow: 'visible' }}>
        {/* `glow` mode draws only the blurred arc — it's rendered behind the
            opaque button so the inner half is masked and only the outer halo shows. */}
        {!glow && <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(0,0,0,0.22)" strokeWidth={stroke} />}
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={V2.brand} strokeWidth={stroke}
          strokeDasharray={c} strokeDashoffset={c * (1 - p / 100)} strokeLinecap="round"
          style={{ transition: 'stroke-dashoffset 0.3s ease',
            ...(glow ? { filter: `drop-shadow(0 0 5px ${V2.brand}) drop-shadow(0 0 3px ${V2.brand})` } : {}) }} />
      </svg>
      {children}
    </div>
  );
}

// Returns a ref to attach to the FIRST selectable item on a screen; once `ready`
// flips true (content loaded), it drops gamepad focus onto that item with a few
// retries to beat Steam's default focus-acquisition — so a direction press moves
// straight into the grid instead of needing a DOWN press out of the header.
function useAutoFocus(ready: boolean, dep?: any) {
  const ref = useRef<any>(null);
  useEffect(() => {
    if (!ready) return;
    const timers = [0, 60, 160, 320].map((d) =>
      setTimeout(() => { try { ref.current?.focus(); } catch { } }, d));
    return () => timers.forEach(clearTimeout);
  }, [ready, dep]);
  return ref;
}

function GameTile({ game, onOpen, onActiveCover, focusRef }:
  { game: LibGame; onOpen: (g: LibGame) => void; onActiveCover: (uri: string | null) => void; focusRef?: React.MutableRefObject<any> }) {
  const wide = !!game.screenshot;
  // Wide cards adopt the screenshot's NATURAL aspect ratio (RomM derives the
  // card width from the cover's true shape at a fixed height); 16:9 until loaded.
  const [shotRatio, setShotRatio] = useState(16 / 9);
  const uriRef = useRef<string | null>(null);
  const [focused, setFocused] = useState(false);
  const [dl, setDl] = useState(!!game.is_downloaded);
  // A collection sync (backend auto-sync or the one-shot batch) downloads through
  // a path that doesn't touch this tile's handlers, so reflect the refreshed
  // prop. Only light the dot — never clear it here — so an in-flight user
  // download isn't visually undone by a transient refetch.
  useEffect(() => { if (game.is_downloaded) setDl(true); }, [game.is_downloaded]);
  const [busy, setBusy] = useState<null | 'download' | 'delete' | 'launch'>(null);
  const [activeDlRomId, setActiveDlRomId] = useState<number>(game.rom_id);
  const dlPct = useDownloadProgress(activeDlRomId, busy === 'download')?.percent ?? null;
  const globalDownloading = useIsDownloading(activeDlRomId);
  const downloading = busy === 'download' || globalDownloading;
  const [confirmDelete, setConfirmDelete] = useState(false);
  const confirmTimer = useRef<any>(null);
  const activate = () => { onActiveCover(uriRef.current); };

  // Multi-disc support on the cover: fetch the on-disk disc list when the tile
  // is first focused (for the disc-picker menu). The badge visibility is driven
  // by the backend is_multi_disc flag so it appears immediately.
  const [discs, setDiscs] = useState<LocalDisc[]>([]);
  const [discLast, setDiscLast] = useState<string>('');
  const discsTried = useRef(false);
  const pickableDiscs = discs.filter((d) => !d.is_m3u);
  const isMultiDisc = !!game.is_multi_disc && dl;
  // A downloaded multi-FILE ROM whose members are regional variants (no disc
  // playlist). Detected once the on-disk entries are fetched (on focus).
  const discsAreRegion = discs.length > 0 && discs.every((d) => d.is_region);
  const isMultiRegion = !!game.region_count && game.region_count > 1;
  const [activeRegionId, setActiveRegionId] = useState<number>(game.rom_id);
  const [downloadedRegionIds, setDownloadedRegionIds] = useState<Set<number>>(new Set());
  const siblingFetchTried = useRef(false);
  const ensureDiscs = async (force?: boolean) => {
    if ((discsTried.current && !force) || !dl) return;
    discsTried.current = true;
    try {
      const r = await getLocalDiscs(game.rom_id);
      setDiscs(r?.success ? (r.discs || []) : []);
      setDiscLast(r?.last || '');
    } catch { /* leave empty */ }
  };
  const ensureSiblings = async (force?: boolean) => {
    if ((siblingFetchTried.current && !force) || !isMultiRegion) return;
    siblingFetchTried.current = true;
    try {
      const r = await getLocalSiblings(game.rom_id);
      if (r?.success) setDownloadedRegionIds(new Set(r.downloaded_ids || []));
    } catch { /* leave empty */ }
  };
  // Hold A to open the disc/region picker; a quick press launches. The OK
  // button's onActivate fires on the PRESS edge on Steam Deck, so we can't let
  // it launch (it would fire before a hold is recognised). Instead, for multi
  // games launch is driven entirely from the release handler: arm a timer on
  // A-down that opens the picker at 500ms; on release, if the timer hasn't fired
  // yet it was a short press → launch. onActivate is suppressed for multi games.
  const isMulti = isMultiRegion || isMultiDisc;
  const longFired = useRef(false);
  const pressTimer = useRef<any>(null);
  const onBtnDown = (e: any) => {
    if (e?.detail?.button === GamepadButton.OK && isMulti) {
      longFired.current = false;
      if (pressTimer.current) clearTimeout(pressTimer.current);
      pressTimer.current = setTimeout(() => {
        longFired.current = true;
        pressTimer.current = null;
        if (isMultiRegion) {
          const allSiblings = game.sibling_roms || [];
          openRegionPicker(game.rom_id, game.name, allSiblings, downloadedRegionIds,
            activeRegionId, handleRegionSelected);
        } else {
          openDiscPicker(game.rom_id, game.name, discs, discLast, setBusy,
            () => ensureDiscs(true));
        }
      }, 500);
    }
  };
  const onBtnUp = (e: any) => {
    if (e?.detail?.button === GamepadButton.OK && isMulti) {
      // Short press (timer still pending) → launch the default; long press
      // already opened the picker, so do nothing.
      if (pressTimer.current) {
        clearTimeout(pressTimer.current); pressTimer.current = null;
        if (!longFired.current) primary();
      }
    }
  };

  // Button scheme (RomM GameActions): A = download (if absent) / launch (if
  // present); X = details; Y = delete. Mouse: overlay buttons mirror these.
  const doDownload = async () => {
    if (busy) return;
    setBusy('download');
    setActiveDlRomId(game.rom_id);
    _setDlActive(game.rom_id, true);
    try {
      const start = await downloadGame(game.rom_id);
      if (!start?.success) { toaster.toast({ title: 'Download failed', body: start?.message || 'Error' }); return; }
      const res = await awaitDownload(game.rom_id);
      if (res.ok) { setDl(true); toaster.toast({ title: 'Downloaded', body: game.name }); }
      else toaster.toast({ title: 'Download failed', body: res.message || 'Error' });
    } catch (e) { toaster.toast({ title: 'Download failed', body: String(e) }); }
    finally { _setDlActive(game.rom_id, false); setBusy(null); }
  };
  const doLaunch = async () => {
    if (busy) return;
    setBusy('launch');
    try {
      const r = await launchGameSmart(game.rom_id);
      if (r?.success) toaster.toast({ title: 'Launching', body: game.name });
      else toaster.toast({ title: 'Launch failed', body: r?.message || 'Error' });
    } catch (e) { toaster.toast({ title: 'Launch failed', body: String(e) }); }
    finally { setBusy(null); }
  };
  const handleRegionSelected = async (selectedRomId: number) => {
    setActiveRegionId(selectedRomId);
    const isMainRom = selectedRomId === game.rom_id;
    const isAlreadyDownloaded = isMainRom ? dl : downloadedRegionIds.has(selectedRomId);
    if (!isAlreadyDownloaded) {
      if (busy) { toaster.toast({ title: 'Busy', body: 'Please wait for the current operation' }); return; }
      setBusy('download');
      setActiveDlRomId(selectedRomId);
      _setDlActive(selectedRomId, true);
      try {
        const start = await downloadGame(selectedRomId);
        if (!start?.success) { toaster.toast({ title: 'Download failed', body: start?.message || 'Error' }); return; }
        const res = await awaitDownload(selectedRomId);
        if (res.ok) {
          setDownloadedRegionIds(prev => new Set([...prev, selectedRomId]));
          if (isMainRom) setDl(true);
          toaster.toast({ title: 'Downloaded', body: game.name });
        } else {
          toaster.toast({ title: 'Download failed', body: res.message || 'Error' });
          return;
        }
      } catch (e) { toaster.toast({ title: 'Download failed', body: String(e) }); return; }
      finally { _setDlActive(selectedRomId, false); setBusy(null); }
    }
    // After download (or if already downloaded): check if this region is multi-disc
    try {
      const localDiscs = await getLocalDiscs(selectedRomId);
      const discList: LocalDisc[] = localDiscs?.success ? (localDiscs.discs || []) : [];
      const pickable = discList.filter((d: LocalDisc) => !d.is_m3u);
      if (discList.length > 1 || pickable.length > 1) {
        openDiscPicker(selectedRomId, game.name, discList, localDiscs?.last || '', setBusy);
      } else {
        await runLaunch(selectedRomId, game.name, null, game.name, setBusy);
      }
    } catch {
      await runLaunch(selectedRomId, game.name, null, game.name, setBusy);
    }
  };
  const doDelete = async () => {
    if (busy) return;
    setBusy('delete');
    try {
      const r = await deleteGame(game.rom_id);
      if (r?.success) { setDl(false); }
      else toaster.toast({ title: 'Delete failed', body: r?.message || 'Error' });
    } catch (e) { toaster.toast({ title: 'Delete failed', body: String(e) }); }
    finally { setConfirmDelete(false); setBusy(null); }
  };
  // Two-step confirm on the cover (matching the details page): the first delete
  // press arms it (button turns into a check), the second within 3s commits.
  const requestDelete = () => {
    if (busy) return;
    if (confirmDelete) { doDelete(); return; }
    setConfirmDelete(true);
    if (confirmTimer.current) clearTimeout(confirmTimer.current);
    confirmTimer.current = setTimeout(() => setConfirmDelete(false), 3000);
  };
  const primary = () => {
    if (dl) doLaunch();
    else doDownload();
  };

  return (
    <Focusable noFocusRing
      ref={focusRef}
      onActivate={() => {
        // Multi games drive launch from the release handler (onBtnUp) so a hold
        // can open the picker without the press-edge activation launching first.
        if (isMulti) { longFired.current = false; return; }
        primary();
      }}
      onClick={primary}
      onButtonDown={onBtnDown}
      onButtonUp={onBtnUp}
      onSecondaryButton={() => onOpen(game)}
      onSecondaryActionDescription="Details"
      onOptionsButton={() => { if (dl) requestDelete(); }}
      onOptionsActionDescription={dl ? (confirmDelete ? 'Confirm delete' : 'Delete') : undefined}
      onOKActionDescription={dl ? (isMultiRegion ? 'Launch (hold: regions)' : isMultiDisc ? (discsAreRegion ? 'Launch (hold: regions)' : 'Launch (hold: discs)') : 'Launch') : 'Download'}
      onFocus={() => { setFocused(true); activate(); ensureDiscs(); ensureSiblings(); }}
      onBlur={() => setFocused(false)}
      onMouseEnter={() => { setFocused(true); activate(); ensureDiscs(); ensureSiblings(); }}
      onMouseLeave={() => setFocused(false)}
      style={{ cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: '7px' }}
    >
      <div style={{
        position: 'relative', overflow: 'hidden',
        // Wide (continue-playing) cards are a fixed-height 16:9 screenshot with
        // natural width; portrait cards keep the cover's 3:4 footprint.
        ...(wide ? { height: '176px', aspectRatio: String(shotRatio) } : {}),
        transform: focused ? 'scale(1.04)' : 'scale(1)',
        transition: 'transform 0.18s ease',
        borderRadius: V2.radiusArt,
        boxShadow: focused
          ? `0 8px 28px rgba(0,0,0,0.4), 0 0 0 2px ${V2.brand}, 0 0 18px rgba(139,116,232,0.6)`
          : 'none',
      }}>
        {wide ? (
          <>
            <ScreenshotArt path={game.screenshot!} onLoaded={(u) => { uriRef.current = u; }} onRatio={setShotRatio} />
            <CoverPip romId={game.rom_id} hasCover={game.has_cover} hidden={focused} />
          </>
        ) : (
          <GameCover romId={game.rom_id} hasCover={game.has_cover}
            onLoaded={(u) => { uriRef.current = u; }} />
        )}
        {/* Platform icon badge — top-right circular scrim with the real RomM
            platform icon (RomM GameCard .r-gc__platform-icon: 7px inset, 3px
            pad, 78% black scrim, 12%-white border, always visible). */}
        {game.platform_slug && (
          <div style={{
            position: 'absolute', top: '7px', right: '7px', zIndex: 2,
            padding: '3px', borderRadius: '50%',
            background: 'rgba(0,0,0,0.78)', border: '1px solid rgba(255,255,255,0.12)',
            lineHeight: 0,
          }}>
            <div style={{
              width: '22px', height: '22px', display: 'flex',
              alignItems: 'center', justifyContent: 'center', color: V2.fg,
            }}>
              <PlatformIcon slug={game.platform_slug} size={22} />
            </div>
          </div>
        )}
        {/* Downloaded status dot — top-left corner, opposite the platform
            icon so the two affordances don't collide. Hidden for multi-region
            games (the region badge replaces it). */}
        {dl && !isMultiRegion && (
          <div style={{
            position: 'absolute', top: '7px', left: '7px', zIndex: 2,
            width: '10px', height: '10px', borderRadius: '50%',
            background: V2.success, boxShadow: '0 0 0 2px rgba(0,0,0,0.45)',
          }} />
        )}

        {/* Region badge — globe icon + region count, top-left corner. Shown for
            multi-region games so the region picker is discoverable. */}
        {isMultiRegion && (
          <div style={{
            position: 'absolute', left: '7px', top: '7px', zIndex: 2,
            display: 'inline-flex', alignItems: 'center', gap: '4px',
            padding: '2px 6px', borderRadius: V2.radiusPill, fontSize: '10px', fontWeight: 600,
            background: 'rgba(0,0,0,0.78)', border: '1px solid rgba(255,255,255,0.12)', color: V2.fg,
            opacity: focused ? 0 : 1, transition: 'opacity 0.18s ease',
          }}>
            <FaGlobe size={9} />{game.region_count}
          </div>
        )}

        {/* Multi-disc badge — a small disc-count pill (RomM-style scrim chip)
            shown for downloaded multi-disc games so the hold-A picker is
            discoverable. Shows the actual count once fetched, otherwise a
            generic disc icon. */}
        {dl && isMultiDisc && (
          <div style={{
            position: 'absolute', left: '7px', bottom: '7px', zIndex: 2,
            display: 'inline-flex', alignItems: 'center', gap: '4px',
            padding: '2px 6px', borderRadius: V2.radiusPill, fontSize: '10px', fontWeight: 600,
            background: 'rgba(0,0,0,0.78)', border: '1px solid rgba(255,255,255,0.12)', color: V2.fg,
            opacity: focused ? 0 : 1, transition: 'opacity 0.18s ease',
          }}>
            {discsAreRegion
              ? <><FaGlobe size={9} />{discs.length || game.disc_count || ''}</>
              : <><FaClone size={9} />{game.disc_count || pickableDiscs.length || ''}</>}
          </div>
        )}

        {/* GameActions overlay — gradient scrim, center primary (download/play),
            bottom Details + Delete; revealed on hover/focus. */}
        <div style={{
          position: 'absolute', inset: 0, borderRadius: V2.radiusArt, pointerEvents: 'none',
          background: 'linear-gradient(to top, rgba(0,0,0,0.78) 0%, rgba(0,0,0,0) 55%)',
          opacity: focused ? 1 : 0, transition: 'opacity 0.18s ease',
        }} />
        {/* Outer-only download glow — a blurred copy of the arc painted BEHIND
            the opaque white button, so the button masks the inner half and only
            the outward halo escapes. */}
        {downloading && (
          <div style={{
            position: 'absolute', top: '50%', left: '50%',
            transform: 'translate(-50%,-50%)', pointerEvents: 'none',
          }}>
            <ProgressRing pct={dlPct} size={48} stroke={4} glow />
          </div>
        )}
        {/* Center primary (A) — emphasized white round button (RomM Play). */}
        <div
          onClick={(e: any) => { e.stopPropagation(); primary(); }}
          style={{
            position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
            ...roundBtn(44, 'emphasized'), boxShadow: '0 2px 10px rgba(0,0,0,0.55)',
            // Stay visible while downloading so the fill ring is always shown,
            // even if focus moves away mid-download.
            opacity: (focused || downloading) ? 1 : 0, transition: 'opacity 0.18s ease',
          }}>
          {/* While downloading, the progress ring rides ON the button's border:
              the 44px button has radius 22, so a 48px ring with a 4px stroke has
              its circle radius at exactly (48-4)/2 = 22, centered on the rim. */}
          {downloading && (
            <div style={{
              position: 'absolute', top: '50%', left: '50%',
              transform: 'translate(-50%,-50%)', pointerEvents: 'none',
            }}>
              <ProgressRing pct={dlPct} size={48} stroke={4} />
            </div>
          )}
          {downloading
            ? <FaDownload size={15} />
            : busy === 'launch'
              ? <FaSync size={16} style={{ animation: 'spin 1s linear infinite' }} />
              : dl ? <FaPlay size={15} style={{ marginLeft: '2px' }} /> : <FaDownload size={15} />}
        </div>
        {/* Bottom row: Details (X) + Delete (Y, when downloaded) — glass buttons. */}
        <div style={{
          position: 'absolute', left: '8px', right: '8px', bottom: '8px',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          opacity: focused ? 1 : 0, transform: focused ? 'translateY(0)' : 'translateY(6px)',
          transition: 'opacity 0.18s ease, transform 0.18s ease',
        }}>
          <div onClick={(e: any) => { e.stopPropagation(); onOpen(game); }} style={roundBtn(30, 'glass')}>
            <FaInfoCircle size={13} />
          </div>
          {dl && (
            <div onClick={(e: any) => { e.stopPropagation(); requestDelete(); }}
              style={{ ...roundBtn(30, 'danger'),
                // Armed state: solid red fill + check glyph, so it's clear the
                // next press commits the delete.
                ...(confirmDelete ? { background: V2.danger, borderColor: V2.danger, color: '#fff' } : {}) }}>
              {busy === 'delete'
                ? <FaSync size={12} style={{ animation: 'spin 1s linear infinite' }} />
                : confirmDelete ? <FaCheck size={12} /> : <FaTrash size={12} />}
            </div>
          )}
        </div>
      </div>
      <div style={{
        fontSize: '11.5px', color: focused ? V2.fg : V2.fg2, textAlign: 'center',
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        transition: 'color 0.18s', padding: '0 1px',
        // Wide cards have no fixed-width parent — pin the label to the art's
        // width so a long name ellipsises instead of widening the card.
        ...(wide ? { width: 0, minWidth: '100%', maxWidth: '100%' } : {}),
      }}>
        {game.name}
      </div>
    </Focusable>
  );
}

// One image fetched by RomM resource path (collection mosaic cells), base64
// via the backend, cached. Renders nothing visible until loaded.
function PathImage({ path }: { path: string }) {
  const [uri, setUri] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    (async () => {
      try { const r = await qGetImage(path); if (alive) setUri(r?.data_uri || null); }
      catch { /* ignore */ }
    })();
    return () => { alive = false; };
  }, [path]);
  return (
    <div style={{ width: '100%', height: '100%', background: V2.coverPlaceholder, overflow: 'hidden' }}>
      {uri && <img src={uri} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />}
    </div>
  );
}

// Collection cover mosaic — 1 cover fills the box, 2+ paint a 2×2 grid
// (RomM CollectionMosaic). Portrait 3:4 to match the game cards.
function CollectionMosaic({ covers }: { covers: string[] }) {
  const cs = (covers || []).slice(0, 4);
  return (
    <div style={{
      position: 'relative', width: '100%', aspectRatio: '3 / 4',
      borderRadius: V2.radiusLg, overflow: 'hidden', background: V2.coverPlaceholder,
      display: 'grid',
      gridTemplateColumns: cs.length <= 1 ? '1fr' : '1fr 1fr',
      gridTemplateRows: cs.length <= 1 ? '1fr' : '1fr 1fr',
    }}>
      {cs.length === 0 ? (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', color: V2.fgMuted }}>
          <FaBookmark size={20} />
        </div>
      ) : cs.map((p, i) => <PathImage key={i} path={p} />)}
    </div>
  );
}

// Shared per-collection sync state, polled ONCE for the whole grid (not one poll
// per tile) from get_service_status, broadcast to every CollectionTile. Lets the
// tiles show the same download ring as game covers while a collection syncs.
interface ColSyncState { state: string; pct: number | null; downloaded?: number; total?: number; }
const _colSync = new Map<string, ColSyncState>();
const _colSyncListeners = new Set<() => void>();
let _colSyncTimer: any = null;
let _colSyncRefs = 0;
async function _colSyncTick() {
  try {
    const st = await getServiceStatus();
    _colSync.clear();
    for (const c of (st?.collections || [])) {
      _colSync.set(c.name, {
        state: c.sync_state,
        pct: typeof c.downloaded_pct === 'number' ? c.downloaded_pct : null,
        downloaded: c.downloaded, total: c.total,
      });
    }
    _colSyncListeners.forEach((l) => { try { l(); } catch { } });
  } catch { /* transient */ }
}
function useCollectionSync(name: string): ColSyncState | undefined {
  const [, force] = useState(0);
  useEffect(() => {
    const l = () => force((n) => n + 1);
    _colSyncListeners.add(l);
    _colSyncRefs++;
    if (!_colSyncTimer) { _colSyncTick(); _colSyncTimer = setInterval(_colSyncTick, 1500); }
    return () => {
      _colSyncListeners.delete(l);
      _colSyncRefs--;
      if (_colSyncRefs <= 0 && _colSyncTimer) { clearInterval(_colSyncTimer); _colSyncTimer = null; }
    };
  }, []);
  return _colSync.get(name);
}

// Collection tile — mosaic cover + kind badge + name/count below, with the
// focus scale + brand glow (RomM CollectionTile).
function CollectionTile({ group, onOpen, focusRef }: { group: LibGroup; onOpen: (g: LibGroup) => void; focusRef?: React.MutableRefObject<any> }) {
  const [focused, setFocused] = useState(false);
  // Virtual (autogenerated) collections are browse/download only: no persistent
  // auto-sync, no local-delete (they aren't tracked by CollectionSyncManager).
  const isVirtual = !!group.virtual;
  // Per-tile auto-sync state (optimistic), so Y toggles sync straight from the
  // collections grid/rail and the green dot reflects it instantly.
  const [synced, setSynced] = useState(!!group.synced);
  // Live sync state from the shared poll — drives the download ring (same as
  // game covers) while this collection is actively downloading.
  const live = useCollectionSync(group.key);
  const syncing = live?.state === 'syncing';
  const syncPct = live?.pct != null ? live.pct
    : (live && live.total ? Math.round(((live.downloaded || 0) / live.total) * 100) : 0);
  const toggleSync = async () => {
    const next = !synced;
    setSynced(next);
    try {
      const ok = await toggleCollectionSync(group.key, next);
      if (ok === false) throw new Error('backend declined');
      toaster.toast({ title: next ? 'Auto-sync on' : 'Auto-sync off', body: group.label });
    } catch (e) {
      setSynced(!next); // revert
      toaster.toast({ title: 'Sync toggle failed', body: String(e) });
    }
  };
  // Collection actions menu (same as the in-collection header menu): one-shot
  // "Sync missing" + destructive "Remove downloaded" (arm→confirm). Reached by
  // HOLDING A on the tile (or right-click / the ⋯ chip for mouse).
  const removeArmed = useRef(false);
  const doTileRemove = async () => {
    try {
      if (synced) { await toggleCollectionSync(group.key, false); setSynced(false); }
      const ok = await deleteCollectionRoms(group.key);
      if (ok === false) throw new Error('backend declined');
    } catch (e) { toaster.toast({ title: 'Remove failed', body: String(e) }); }
  };
  const openMenu = () => {
    const armed = removeArmed.current;
    showContextMenu(
      <Menu label={group.label} onCancel={() => { removeArmed.current = false; }}>
        <MenuItem onSelected={async () => {
          const res = await getLibraryGames('collection', group.key).catch(() => null);
          const missing = res?.success ? (res.games || []).filter((g: LibGame) => !g.is_downloaded).map((g: LibGame) => g.rom_id) : [];
          if (!missing.length) { toaster.toast({ title: 'Nothing to sync', body: 'All games are already downloaded' }); return; }
          toaster.toast({ title: 'Syncing collection', body: `Downloading ${missing.length} game${missing.length === 1 ? '' : 's'}` });
          const ok = await downloadBatch(missing, 3, () => { });
          toaster.toast({ title: 'Sync complete', body: `${ok} of ${missing.length} downloaded` });
        }}>{isVirtual ? 'Download missing' : 'Sync missing'}</MenuItem>
        {!isVirtual && (
          <MenuItem tone="destructive" onSelected={() => {
            if (!armed) {
              removeArmed.current = true;
              setTimeout(() => { removeArmed.current = false; }, 4000);
              requestAnimationFrame(openMenu);
            } else { removeArmed.current = false; doTileRemove(); }
          }}>{armed ? 'Confirm remove' : 'Remove downloaded'}</MenuItem>
        )}
      </Menu>,
    );
  };

  // Distinguish tap (open) from hold (actions menu) on the A button. onButtonDown
  // starts the hold timer; onActivate (fires on release) opens only if the hold
  // didn't already trigger the menu. Repeats are ignored so the timer fires once.
  const holdTimer = useRef<any>(null);
  const held = useRef(false);
  const onBtnDown = (e: any) => {
    if (e?.detail?.button !== GamepadButton.OK || e?.detail?.is_repeat) return;
    held.current = false;
    clearTimeout(holdTimer.current);
    holdTimer.current = setTimeout(() => { held.current = true; openMenu(); }, 500);
  };
  const onActivate = () => {
    clearTimeout(holdTimer.current);
    if (held.current) { held.current = false; return; } // hold opened the menu
    onOpen(group);
  };

  const badge = group.kind === 'smart' ? { label: 'SMART', bg: V2.brandHover }
    : group.kind === 'virtual' ? { label: 'VIRTUAL', bg: V2.brandHover }
    : group.kind === 'favorite' ? { label: '★', bg: '#ff4f6b' }
    : null;
  return (
    <Focusable noFocusRing
      ref={focusRef}
      onClick={() => onOpen(group)}
      onActivate={onActivate}
      onButtonDown={onBtnDown}
      onOKActionDescription="Open   ·   Hold: Actions"
      onOptionsButton={isVirtual ? undefined : toggleSync}
      onOptionsActionDescription={isVirtual ? undefined : (synced ? 'Stop syncing' : 'Sync collection')}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      onMouseEnter={() => setFocused(true)} onMouseLeave={() => setFocused(false)}
      style={{ cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: '8px' }}
    >
      <div style={{
        position: 'relative', borderRadius: V2.radiusLg,
        transform: focused ? 'scale(1.05)' : 'scale(1)', transition: 'transform 0.18s ease',
        boxShadow: focused
          ? `0 8px 28px rgba(0,0,0,0.4), 0 0 0 2px ${V2.brand}, 0 0 18px rgba(139,116,232,0.55)`
          : '0 2px 8px rgba(0,0,0,0.35)',
      }}>
        <CollectionMosaic covers={group.covers || []} />
        {/* Download ring + glyph while syncing — mirrors the game-cover affordance
            so a downloading collection reads the same as a downloading game. */}
        {syncing && (
          <>
            <div style={{
              position: 'absolute', inset: 0, borderRadius: V2.radiusLg,
              background: 'rgba(0,0,0,0.45)', pointerEvents: 'none', zIndex: 1,
            }} />
            <div style={{
              position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
              zIndex: 2, ...roundBtn(44, 'emphasized'), boxShadow: '0 2px 10px rgba(0,0,0,0.55)',
            }}>
              <div style={{
                position: 'absolute', top: '50%', left: '50%',
                transform: 'translate(-50%,-50%)', pointerEvents: 'none',
              }}>
                <ProgressRing pct={syncPct} size={48} stroke={4} />
              </div>
              <FaDownload size={15} />
            </div>
          </>
        )}
        {/* Synced status dot — same green dot as downloaded games, placed
            top-right so it never collides with the kind badge (top-left).
            Hidden while the ring is showing. */}
        {synced && !syncing && (
          <div style={{
            position: 'absolute', top: '7px', right: '7px', zIndex: 2,
            width: '10px', height: '10px', borderRadius: '50%',
            background: V2.success, boxShadow: '0 0 0 2px rgba(0,0,0,0.45)',
          }} />
        )}
        {badge && (
          <span style={{
            position: 'absolute', top: '6px', left: '6px',
            fontSize: '9px', fontWeight: 800, letterSpacing: '0.04em',
            padding: '2px 7px', borderRadius: V2.radiusChip, color: '#fff',
            background: badge.bg, boxShadow: '0 1px 3px rgba(0,0,0,0.5)',
          }}>{badge.label}</span>
        )}
        {/* Actions hint — a ⋯ chip on focus/hover signals the long-press (hold A)
            / right-click actions menu exists; also clickable for mouse users. */}
        {focused && !syncing && (
          <div
            onClick={(e: any) => { e.stopPropagation(); openMenu(); }}
            style={{
              position: 'absolute', bottom: '6px', right: '6px', zIndex: 3,
              display: 'flex', alignItems: 'center', gap: '4px',
              padding: '2px 6px', borderRadius: V2.radiusChip,
              background: 'rgba(0,0,0,0.62)', color: V2.fg,
              fontSize: '9px', fontWeight: 700, letterSpacing: '0.02em',
              boxShadow: '0 1px 3px rgba(0,0,0,0.5)', pointerEvents: 'auto',
            }}>
            <FaEllipsisH size={9} /><span>HOLD</span>
          </div>
        )}
      </div>
      <div>
        <div style={{
          fontSize: '12.5px', fontWeight: 600, color: focused ? V2.fg : V2.fg2,
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          transition: 'color 0.18s',
        }}>{group.label}</div>
        <div style={{ fontSize: '11px', color: V2.fgMuted }}>
          {group.count} {group.count === 1 ? 'game' : 'games'}
        </div>
      </div>
    </Focusable>
  );
}

// True RomM platform icon, served by the RomM server at
// /assets/platforms/{slug}.svg (the RPlatformIcon fallback chain:
// fsSlug.svg → fsSlug.ico → slug.svg → slug.ico). Each candidate is fetched
// via the backend get_image RPC; falls back to a gamepad glyph if none load.
function PlatformIcon({ slug, fsSlug, size }: { slug?: string | null; fsSlug?: string | null; size: number }) {
  const [uri, setUri] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    const fs = (fsSlug || slug || '').toLowerCase().trim();
    const s = (slug || '').toLowerCase().trim();
    const cands: string[] = [];
    if (fs) cands.push(`/assets/platforms/${fs}.svg`, `/assets/platforms/${fs}.ico`);
    if (s && s !== fs) cands.push(`/assets/platforms/${s}.svg`, `/assets/platforms/${s}.ico`);
    cands.push('/assets/platforms/default.ico');
    (async () => {
      for (const c of cands) {
        try {
          const r = await qGetImage(c);
          if (!alive) return;
          if (r?.data_uri) { setUri(r.data_uri); return; }
        } catch { /* try next */ }
      }
      if (alive) setFailed(true);
    })();
    return () => { alive = false; };
  }, [slug, fsSlug]);
  if (uri) return <img src={uri} style={{ width: '100%', height: '100%', objectFit: 'contain' }} />;
  if (failed || (!slug && !fsSlug)) return <FaGamepad size={Math.round(size * 0.75)} />;
  return null; // loading
}

// Platform tile — centered icon-led card (RomM PlatformTile): bg-elevated
// card, large icon on top, name + count, focus brand glow.
function PlatformTile({ group, onOpen, focusRef }: { group: LibGroup; onOpen: (g: LibGroup) => void; focusRef?: React.MutableRefObject<any> }) {
  const [focused, setFocused] = useState(false);
  return (
    <Focusable noFocusRing
      ref={focusRef}
      onActivate={() => onOpen(group)} onClick={() => onOpen(group)}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      onMouseEnter={() => setFocused(true)} onMouseLeave={() => setFocused(false)}
      style={{
        cursor: 'pointer', display: 'flex', flexDirection: 'column', alignItems: 'center',
        gap: '12px', padding: '24px 16px 18px',
        background: focused ? V2.surface : 'rgba(255,255,255,0.045)',
        border: `1px solid ${focused ? V2.brand : V2.border}`, borderRadius: V2.radiusCard,
        transition: 'background 0.15s, border-color 0.15s, transform 0.15s',
        boxShadow: focused
          ? `0 8px 28px rgba(0,0,0,0.35), 0 0 0 2px ${V2.brand}, 0 0 18px rgba(139,116,232,0.55)`
          : 'none',
      }}
    >
      <div style={{
        width: '72px', height: '72px', display: 'grid', placeItems: 'center',
        color: focused ? V2.brandHover : V2.fg2, opacity: focused ? 1 : 0.9,
        transform: focused ? 'scale(1.05)' : 'scale(1)', transition: 'transform 0.15s, color 0.15s',
      }}>
        <PlatformIcon slug={group.slug} fsSlug={group.fs_slug} size={72} />
      </div>
      <div style={{ fontSize: '12px', fontWeight: 600, textAlign: 'center', lineHeight: 1.35, color: focused ? V2.fg : V2.fg2 }}>
        {group.label}
      </div>
      <div style={{ fontSize: '11px', color: V2.fgMuted }}>
        {group.count} {group.count === 1 ? 'game' : 'games'}
        {group.downloaded != null && group.downloaded > 0 && (
          <span style={{ color: V2.success }}>{`  ·  ${group.downloaded} ↓`}</span>
        )}
      </div>
    </Focusable>
  );
}

// RomM AppNav — fixed glass top bar: logo (left) · centered tab pill
// (Home/Platforms/Collections/Search) · right cluster. Geometry is grid
// 1fr/auto/1fr so the pill stays viewport-centered (AppNav.vue). The tab
// pill is RSliderBtnGroup's "tab" variant: surface bg + strong border, pill
// radius, and the ACTIVE tab is a solid white (--r-color-fg) pill with dark
// (--r-color-bg) text.
type NavId = 'home' | 'platforms' | 'collections' | 'search';
function V2NavBar({ active, onTab, activeRef }: { active: NavId; onTab: (id: NavId) => void; activeRef?: React.MutableRefObject<any> }) {
  const [iso, setIso] = useState<string | null>(null);
  const [word, setWord] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    (async () => {
      try { const a = await getImage('/assets/isotipo.svg'); if (alive) setIso(a?.data_uri || null); } catch { }
      try { const b = await getImage('/assets/logotipo.svg'); if (alive) setWord(b?.data_uri || null); } catch { }
    })();
    return () => { alive = false; };
  }, []);
  const tabs: { id: NavId; label: string; Icon: any }[] = [
    { id: 'home', label: 'Home', Icon: FaHome },
    { id: 'platforms', label: 'Platforms', Icon: FaGamepad },
    { id: 'collections', label: 'Collections', Icon: FaBookmark },
    { id: 'search', label: 'Search', Icon: FaSearch },
  ];

  // Sliding active indicator (RSliderBtnGroup): one white pill whose left/width
  // animates between the active tab's measured position, instead of toggling a
  // background per button.
  const btnRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [ind, setInd] = useState<{ left: number; width: number } | null>(null);
  const [shown, setShown] = useState(false); // drives the first-load grow/fade-in
  const activeIdx = tabs.findIndex((t) => t.id === active);
  useEffect(() => {
    const el = btnRefs.current[activeIdx];
    if (el) {
      setInd({ left: el.offsetLeft, width: el.offsetWidth });
      // Next frame: flip from the collapsed/transparent initial state to full so
      // the indicator animates into place on first paint.
      requestAnimationFrame(() => setShown(true));
    }
  }, [activeIdx]);

  return (
    <div style={{
      position: 'sticky', top: 0, zIndex: 50, height: '58px',
      display: 'grid', gridTemplateColumns: '1fr auto 1fr', alignItems: 'center',
      padding: '0 20px', background: 'rgba(7,7,15,0.78)',
      backdropFilter: 'blur(20px)', WebkitBackdropFilter: 'blur(20px)',
      borderBottom: `1px solid ${V2.border}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
        {iso && <img src={iso} style={{ width: '32px', height: '32px', display: 'block' }} />}
        {word && <img src={word} style={{ height: '22px', width: 'auto', display: 'block' }} />}
      </div>
      <div style={{ justifySelf: 'center', display: 'flex', alignItems: 'center', gap: '10px' }}>
      <Bumper label="L1" />
      <Focusable noFocusRing flow-children="horizontal" style={{
        position: 'relative', display: 'flex', gap: '2px', padding: '4px',
        background: V2.surface, border: `1px solid ${V2.borderStrong}`, borderRadius: V2.radiusPill,
      }}>
        {/* Sliding indicator */}
        {ind && (
          <div style={{
            position: 'absolute', top: '4px', bottom: '4px',
            left: `${ind.left}px`, width: `${ind.width}px`,
            background: V2.fg, borderRadius: V2.radiusPill, zIndex: 0,
            opacity: shown ? 1 : 0,
            transform: shown ? 'scaleX(1)' : 'scaleX(0.6)', transformOrigin: 'center',
            transition: 'left 0.28s cubic-bezier(0.22,1,0.36,1), width 0.28s cubic-bezier(0.22,1,0.36,1), opacity 0.28s ease, transform 0.28s cubic-bezier(0.22,1,0.36,1)',
          }} />
        )}
        {tabs.map(({ id, label, Icon }, i) => {
          const on = active === id;
          return (
            <Focusable noFocusRing key={id} ref={on && activeRef ? activeRef : undefined} onActivate={() => onTab(id)} onClick={() => onTab(id)}>
              <div ref={(el) => { btnRefs.current[i] = el; }}
                style={{
                  position: 'relative', zIndex: 1,
                  display: 'flex', alignItems: 'center', gap: '7px', padding: '7px 18px',
                  borderRadius: V2.radiusPill, fontSize: '13.5px', cursor: 'pointer',
                  fontWeight: on ? 600 : 500, color: on ? V2.bg : V2.fg2,
                  transition: 'color 0.2s ease',
                }}>
                <Icon size={12} /><span>{label}</span>
              </div>
            </Focusable>
          );
        })}
      </Focusable>
      <Bumper label="R1" />
      </div>
      <div style={{ justifySelf: 'end' }} />
    </div>
  );
}

// Bumper keycap hint (L1 / R1) flanking the nav pill — signals that the
// shoulder buttons page through the tabs.
function Bumper({ label }: { label: string }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
      fontSize: '10px', fontWeight: 700, letterSpacing: '0.03em', color: V2.fg2,
      padding: '3px 8px', borderRadius: V2.radiusChip, background: V2.surface,
      border: `1px solid ${V2.borderStrong}`, boxShadow: '0 1px 2px rgba(0,0,0,0.4)',
      lineHeight: 1, whiteSpace: 'nowrap',
    }}>{label}</span>
  );
}

// RomM RTabNav "underlined" variant — tabs over a bottom border with a 2px
// brand underline that slides between the active tab (GameDetails tab strip).
function V2TabNav({ tabs, active, onTab }:
  { tabs: { id: string; label: string }[]; active: string; onTab: (id: string) => void }) {
  const refs = useRef<(HTMLDivElement | null)[]>([]);
  const [ind, setInd] = useState<{ left: number; width: number } | null>(null);
  const idx = tabs.findIndex((t) => t.id === active);
  useEffect(() => {
    const el = refs.current[idx];
    if (el) setInd({ left: el.offsetLeft, width: el.offsetWidth });
  }, [idx, tabs.length]);
  return (
    <div style={{ position: 'relative', display: 'flex', gap: '2px', borderBottom: `1px solid ${V2.borderStrong}` }}>
      {tabs.map((t, i) => {
        const on = active === t.id;
        return (
          <Focusable noFocusRing key={t.id} onActivate={() => onTab(t.id)} onClick={() => onTab(t.id)}>
            <div ref={(el) => { refs.current[i] = el; }}
              style={{
                padding: '8px 18px', fontSize: '13px', cursor: 'pointer',
                fontWeight: 500, color: on ? V2.fg : V2.fgMuted, transition: 'color 0.15s ease',
              }}>
              {t.label}
            </div>
          </Focusable>
        );
      })}
      {ind && (
        <div style={{
          position: 'absolute', bottom: '-1px', height: '2px', borderRadius: '2px 2px 0 0',
          left: `${ind.left}px`, width: `${ind.width}px`, background: V2.brand,
          transition: 'left 0.25s cubic-bezier(0.22,1,0.36,1), width 0.25s cubic-bezier(0.22,1,0.36,1)',
        }} />
      )}
    </div>
  );
}

// RomM RBtn language: 8px rounded-rect (not a pill), three tones — filled
// brand (primary CTA), translucent surface (tonal), and bare text. Focus/hover
// brightens + paints the brand ring (matches RBtn's currentColor overlay +
// focus glow).
function V2Button({ children, onClick, variant = 'tonal', color, disabled }:
  { children: any; onClick: () => void; variant?: 'primary' | 'danger' | 'tonal' | 'text'; color?: string; disabled?: boolean }) {
  const [active, setActive] = useState(false);
  const base: any = {
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
    height: '36px', padding: '0 16px', borderRadius: V2.radiusMd, fontSize: '14px',
    fontWeight: 600, whiteSpace: 'nowrap', border: '1px solid transparent',
    cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.5 : 1,
    transition: 'background 0.15s, box-shadow 0.15s, filter 0.15s',
  };
  const tone =
    variant === 'primary' ? { background: V2.brand, color: '#fff' }
    : variant === 'danger' ? { background: V2.danger, color: '#fff' }
    : variant === 'tonal' ? { background: V2.surface, color: color || V2.fg, border: `1px solid ${V2.border}` }
    : { background: 'transparent', color: color || V2.fg2 };
  const glow = active && !disabled
    ? { boxShadow: `0 0 0 2px ${V2.brand}`, filter: 'brightness(1.12)' }
    : {};
  return (
    <Focusable noFocusRing
      className="romm-btn"
      onActivate={() => !disabled && onClick()}
      onClick={() => !disabled && onClick()}
      onFocus={() => setActive(true)} onBlur={() => setActive(false)}
      onMouseEnter={() => setActive(true)} onMouseLeave={() => setActive(false)}
      style={{ ...base, ...tone, ...glow }}
    >
      {children}
    </Focusable>
  );
}

// GameActionButton — RomM's GameActionBtn vocabulary (the action ribbon in the
// GameDetails header), distinct from V2Button's RBtn rounded-rect. Two shapes:
//   • emphasized + label → white pill CTA (#fff / #111117), used by Play / the
//     primary Download (overlay-emphasis tokens).
//   • surface, icon-only  → circular translucent-grey glass button matching the
//     page background (RTag tokens); used for Delete / secondary actions.
// `danger` is a filled-danger pill for the delete-confirm step. Pill radius
// throughout; controller focus paints a brand ring + slight scale.
function GameActionButton({ icon, label, onClick, variant = 'surface', accent, disabled, progress,
  onOptionsButton, optionsHint }:
  { icon: any; label?: string; onClick: () => void;
    variant?: 'emphasized' | 'surface' | 'danger'; accent?: 'danger'; disabled?: boolean;
    progress?: number | null; onOptionsButton?: () => void; optionsHint?: boolean }) {
  const [active, setActive] = useState(false);
  const labelled = !!label;
  const hasProgress = typeof progress === 'number';
  const pct = hasProgress ? Math.max(0, Math.min(100, progress as number)) : 0;
  // One-shot width "pop" when a download begins: as the label grows from
  // "Download" to "Downloading… 0%", the button overshoots wider then settles.
  const [pop, setPop] = useState(false);
  const wasProg = useRef(false);
  useEffect(() => {
    if (hasProgress && !wasProg.current) {
      wasProg.current = true;
      setPop(true);
      const t = setTimeout(() => setPop(false), 480);
      return () => clearTimeout(t);
    }
    if (!hasProgress) wasProg.current = false;
  }, [hasProgress]);
  const base: any = {
    position: 'relative', overflow: 'hidden',
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
    height: '44px', borderRadius: V2.radiusPill, fontSize: '14px', fontWeight: 600,
    whiteSpace: 'nowrap', border: '1px solid transparent',
    cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.5 : 1,
    ...(labelled ? { padding: '0 24px' } : { width: '44px' }),
    transition: 'background 0.15s, color 0.15s, transform 0.15s, box-shadow 0.15s, border-color 0.15s',
    // Grow rightward from the left edge as the label widens — no overshoot.
    ...(pop ? { animation: 'dlGrow 0.4s ease', transformOrigin: 'left center' } : {}),
  };
  // Danger-accented surface (Delete): red icon + red-tinted surface/border that
  // intensifies on focus, so it reads as destructive without being a filled CTA.
  const dangerSurface = accent === 'danger';
  const tone =
    variant === 'emphasized'
      ? { background: active ? '#e6e6e6' : '#ffffff', color: '#111117', borderColor: '#ffffff' }
    : variant === 'danger'
      ? { background: V2.danger, color: '#fff', borderColor: V2.danger }
    : dangerSurface
      ? { background: active ? 'rgba(255,80,80,0.18)' : 'rgba(255,80,80,0.10)',
          color: V2.danger, borderColor: active ? V2.danger : 'rgba(255,80,80,0.40)' }
    : { background: active ? V2.surfaceHover : V2.surface,
        color: active ? V2.fg : V2.fg2, borderColor: V2.borderStrong };
  const ring = dangerSurface ? V2.danger : V2.brand;
  const glow = active && !disabled ? { boxShadow: `0 0 0 2px ${ring}` } : {};
  return (
    <Focusable noFocusRing
      onActivate={() => !disabled && onClick()}
      onClick={() => !disabled && onClick()}
      onOptionsButton={onOptionsButton ? () => !disabled && onOptionsButton() : undefined}
      onOptionsActionDescription={onOptionsButton ? 'Select disc' : undefined}
      onFocus={() => setActive(true)} onBlur={() => setActive(false)}
      onMouseEnter={() => setActive(true)} onMouseLeave={() => setActive(false)}
      style={{ ...base, ...tone, ...glow }}
    >
      {/* Download fill — a brand-tinted bar sweeping left→right behind the
          label, tracking the live download percentage. */}
      {hasProgress && (
        <div style={{
          position: 'absolute', left: 0, top: 0, bottom: 0, width: `${pct}%`,
          background: variant === 'emphasized' ? 'rgba(139,116,232,0.30)' : 'rgba(139,116,232,0.45)',
          // Smoothly sweep the semi-transparent fill across the white button, and
          // fade it in on the first frame so the colour change is animated too.
          transition: 'width 0.45s cubic-bezier(0.22,1,0.36,1), background 0.3s ease',
          animation: 'dlFillIn 0.3s ease', pointerEvents: 'none',
        }} />
      )}
      <span style={{ position: 'relative', zIndex: 1, display: 'inline-flex', alignItems: 'center', gap: '8px' }}>
        {icon}
        {label && <span>{label}</span>}
        {optionsHint && (
          <span style={{
            marginLeft: '2px', fontSize: '11px', fontWeight: 700, lineHeight: 1,
            padding: '2px 5px', borderRadius: '6px',
            background: variant === 'emphasized' ? 'rgba(17,17,23,0.12)' : V2.surfaceHover,
            color: variant === 'emphasized' ? '#111117' : V2.fgMuted,
          }}>Y</span>
        )}
      </span>
      <style>{`
        @keyframes dlGrow { from { transform: scaleX(0.72); } to { transform: scaleX(1); } }
        @keyframes dlValIn { from { opacity: 0; transform: translateX(-6px); } to { opacity: 1; transform: translateX(0); } }
        @keyframes dlFillIn { from { opacity: 0; } to { opacity: 1; } }
      `}</style>
    </Focusable>
  );
}

// ── GameDetails sub-components (faithful to RomM's OverviewTab / MetadataTab) ──

// Uppercase eyebrow heading for an overview section (RomM
// .overview-tab__section-heading).
function SectionHeading({ icon, children }: { icon?: any; children: any }) {
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: '6px', margin: 0,
      fontSize: '11px', fontWeight: 700, letterSpacing: '0.1em',
      textTransform: 'uppercase', color: V2.fgFaint,
    }}>
      {icon}{children}
    </div>
  );
}

// PlayerCountBadge — pill with a player icon whose glyph scales with the
// max player count parsed from the free-form string (RomM PlayerCountBadge).
function PlayerCountBadge({ value }: { value: string }) {
  const nums = (value.match(/\d+/g) || []).map(Number);
  const n = nums.length ? Math.max(...nums) : null;
  const label = n === 1 ? 'Single player' : (n && n > 1) ? `${value} players` : value;
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: '8px', padding: '5px 12px 5px 10px',
      background: V2.surface, border: `1px solid ${V2.borderStrong}`, borderRadius: V2.radiusPill,
      fontSize: '12px', color: V2.fg2,
    }}>
      <FaUsers size={14} color={V2.brand} />
      <span style={{ fontWeight: 600, letterSpacing: '0.01em' }}>{label}</span>
    </div>
  );
}

// AgeRatingBadges — 44px icon badges from the IGDB rating-icon CDN (loaded
// directly), falling back to a shield text chip when the icon 404s (RomM
// AgeRatingBadges).
function AgeRatingBadge({ item }: { item: { category: string; rating: string; icon_url: string | null } }) {
  const [failed, setFailed] = useState(false);
  const label = item.category ? `${item.category}: ${item.rating}` : item.rating;
  if (item.icon_url && !failed) {
    return <img src={item.icon_url} alt={label} title={label} loading="lazy"
      onError={() => setFailed(true)}
      style={{ width: '44px', height: '44px', objectFit: 'contain', borderRadius: V2.radiusSm,
        background: V2.surface, padding: '3px', border: `1px solid ${V2.border}` }} />;
  }
  return (
    <span title={label} style={{
      display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '4px 10px',
      background: V2.surface, border: `1px solid ${V2.borderStrong}`, borderRadius: V2.radiusChip,
      fontSize: '11.5px', fontWeight: 600, color: V2.fg2, letterSpacing: '0.02em',
    }}>🛡 {label}</span>
  );
}
function AgeRatingBadges({ items }: { items: any[] }) {
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px', alignItems: 'center' }}>
      {items.map((b, i) => <AgeRatingBadge key={i} item={b} />)}
    </div>
  );
}

// HLTBStrip — up to four columns (main story / +extra / completionist / all
// styles). Durations are seconds → hours rounded to 0.5h (RomM HLTBStrip).
function HLTBStrip({ hltb }: { hltb: any }) {
  const fmtHours = (secs?: number | null): string | null => {
    if (!secs || secs <= 0) return null;
    const hours = secs / 3600;
    if (hours < 1) { const m = Math.round(secs / 60); return m > 0 ? `${m}m` : null; }
    return `${Math.round(hours * 2) / 2}h`;
  };
  const candidates: [string, number | undefined, number | undefined][] = [
    ['Main Story', hltb?.main_story, hltb?.main_story_count],
    ['Main + Extra', hltb?.main_plus_extra, hltb?.main_plus_extra_count],
    ['Completionist', hltb?.completionist, hltb?.completionist_count],
    ['All Styles', hltb?.all_styles, hltb?.all_styles_count],
  ];
  const entries = candidates
    .map(([label, v, c]) => ({ label, value: fmtHours(v), count: c }))
    .filter((e) => e.value);
  if (!entries.length) return null;
  return (
    <div style={{
      display: 'flex', alignItems: 'stretch', background: V2.bgElevated,
      border: `1px solid ${V2.border}`, borderRadius: V2.radiusLg, padding: '14px 0', maxWidth: '720px',
    }}>
      {entries.map((e, i) => (
        <div key={e.label} style={{
          flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px',
          padding: '0 12px', borderRight: i < entries.length - 1 ? `1px solid ${V2.border}` : 'none',
        }}>
          <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase', color: V2.fgFaint, textAlign: 'center' }}>{e.label}</div>
          <div style={{ fontSize: '20px', fontWeight: 700, color: V2.fg }}>{e.value}</div>
          {e.count ? <div style={{ fontSize: '10px', color: V2.fgFaint }}>{e.count.toLocaleString()} players</div> : null}
        </div>
      ))}
    </div>
  );
}

// InfoGrid — two-column section grid; each section is icon + uppercase label
// over a row of chips (RomM InfoGrid).
function InfoGrid({ sections }: { sections: { label: string; items: string[]; icon?: any }[] }) {
  const visible = sections.filter((s) => s.items.length > 0);
  if (!visible.length) return null;
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, auto))', gap: '18px 24px', width: '100%' }}>
      {visible.map((s) => (
        <div key={s.label}>
          <div style={{
            display: 'inline-flex', alignItems: 'center', gap: '6px', marginBottom: '8px',
            fontSize: '10.5px', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', color: V2.fgFaint,
          }}>
            {s.icon && <span style={{ color: V2.brand, display: 'inline-flex' }}>{s.icon}</span>}
            <span>{s.label}</span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
            {s.items.map((it, i) => (
              <span key={i} style={{
                background: V2.surface, border: `1px solid ${V2.borderStrong}`, borderRadius: V2.radiusChip,
                padding: '4px 10px', fontSize: '11.5px', fontWeight: 500, color: V2.fg2,
              }}>{it}</span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// RelatedGameCard — static cover tile using IGDB's external cover URL (loads
// directly, no auth). Cover + truncated label, focus lift (RomM RelatedGameCard
// in GameCard static mode). Non-navigating (synthetic).
function RelatedGameCard({ game }: { game: { id: number; name: string; cover_url?: string | null } }) {
  const [focused, setFocused] = useState(false);
  // IGDB thumb URLs are tiny (t_thumb); request the bigger cover art variant.
  const cover = game.cover_url ? game.cover_url.replace('/t_thumb/', '/t_cover_big/') : null;
  return (
    <Focusable noFocusRing
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      onMouseEnter={() => setFocused(true)} onMouseLeave={() => setFocused(false)}
      style={{ width: '110px', flexShrink: 0, display: 'flex', flexDirection: 'column', gap: '6px', cursor: 'default' }}
    >
      <div style={{
        width: '100%', aspectRatio: '3 / 4', borderRadius: V2.radiusArt, overflow: 'hidden',
        background: V2.coverPlaceholder, display: 'flex', alignItems: 'center', justifyContent: 'center',
        transform: focused ? 'scale(1.05)' : 'scale(1)', transition: 'transform 0.18s ease, box-shadow 0.18s ease',
        boxShadow: focused ? `0 8px 24px rgba(0,0,0,0.4), 0 0 0 2px ${V2.brand}` : 'none',
      }}>
        {cover
          ? <img src={cover} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
          : <span style={{ color: V2.fgMuted, fontSize: '10px', padding: '0 6px', textAlign: 'center' }}>{game.name}</span>}
      </div>
      <div style={{
        fontSize: '11px', color: focused ? V2.fg : V2.fg2, textAlign: 'center',
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
      }}>{game.name}</div>
    </Focusable>
  );
}

// One related-games section: eyebrow heading + flex-wrap row of cards.
function RelatedSection({ icon, title, items }: { icon: any; title: string; items: any[] }) {
  if (!items?.length) return null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      <SectionHeading icon={icon}>{title}</SectionHeading>
      <Focusable noFocusRing flow-children="horizontal" style={{ display: 'flex', flexWrap: 'wrap', gap: '14px 16px', padding: '6px 6px 4px' }}>
        {items.map((g) => <RelatedGameCard key={g.id ?? g.name} game={g} />)}
      </Focusable>
    </div>
  );
}

// Metadata-provider registry (RomM providers.ts): id field · brand colour ·
// logo asset · external URL builder.
const PROVIDERS: { key: string; name: string; color: string; logo: string; url: ((id: any) => string) | null }[] = [
  { key: 'igdb_id', name: 'IGDB', color: '#6366f1', logo: '/assets/scrappers/igdb.png', url: (id) => `https://www.igdb.com/search?type=1&q=${id}` },
  { key: 'moby_id', name: 'MobyGames', color: '#f59e0b', logo: '/assets/scrappers/moby.png', url: (id) => `https://www.mobygames.com/game/${id}/` },
  { key: 'ss_id', name: 'ScreenScraper', color: '#3b82f6', logo: '/assets/scrappers/ss.png', url: (id) => `https://www.screenscraper.fr/gameinfos.php?gameid=${id}` },
  { key: 'ra_id', name: 'RetroAchievements', color: '#ef4444', logo: '/assets/scrappers/ra.png', url: (id) => `https://retroachievements.org/game/${id}` },
  { key: 'sgdb_id', name: 'SteamGridDB', color: '#0ea5e9', logo: '/assets/scrappers/sgdb.png', url: (id) => `https://www.steamgriddb.com/game/${id}` },
  { key: 'launchbox_id', name: 'LaunchBox', color: '#8b5cf6', logo: '/assets/scrappers/launchbox.png', url: (id) => `https://gamesdb.launchbox-app.com/games/dbid/${id}` },
  { key: 'hasheous_id', name: 'Hasheous', color: '#6b7280', logo: '/assets/scrappers/hasheous.png', url: null },
  { key: 'flashpoint_id', name: 'Flashpoint Archive', color: '#f97316', logo: '/assets/scrappers/flashpoint.png', url: null },
  { key: 'hltb_id', name: 'HowLongToBeat', color: '#22c55e', logo: '/assets/scrappers/hltb.png', url: (id) => `https://howlongtobeat.com/game/${id}` },
];

// ProviderCard — logo + name + linked id (or "Not linked"); clickable when a
// URL resolves. Logo is base64'd via get_image (RomM-served asset).
function ProviderCard({ p, id }: { p: typeof PROVIDERS[number]; id: any }) {
  const [focused, setFocused] = useState(false);
  const logo = useRommImage(p.logo);
  const linked = id !== null && id !== undefined && id !== '' && id !== 0;
  const href = linked && p.url ? p.url(id) : null;
  const open = () => { if (href) try { Navigation?.NavigateToExternalWeb?.(href); } catch { /* ignore */ } };
  return (
    <Focusable noFocusRing
      onActivate={open} onClick={open}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      onMouseEnter={() => setFocused(true)} onMouseLeave={() => setFocused(false)}
      style={{
        display: 'flex', flexDirection: 'column', gap: '6px', padding: '12px 14px',
        background: V2.bgElevated, borderRadius: V2.radiusMd, color: V2.fg,
        border: `1px solid ${focused && href ? p.color : V2.border}`,
        opacity: linked ? 1 : 0.55, cursor: href ? 'pointer' : 'default',
        transform: focused && href ? 'translateY(-1px)' : 'none',
        transition: 'background 0.15s, border-color 0.15s, transform 0.15s',
      }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        {logo && <img src={logo} alt={p.name} style={{ width: '16px', height: '16px', objectFit: 'contain', borderRadius: '2px' }} />}
        <span style={{ flex: 1, fontSize: '12.5px', fontWeight: 600, color: V2.fg }}>{p.name}</span>
        {href && <FaExternalLinkAlt size={11} color={V2.fgMuted} />}
      </div>
      <div style={{ fontSize: '11.5px', color: V2.fg2, fontVariantNumeric: 'tabular-nums' }}>
        {linked ? String(id) : <span style={{ fontStyle: 'italic', color: V2.fgFaint }}>Not linked</span>}
      </div>
    </Focusable>
  );
}

// MetadataTab — file info · hashes (click-to-copy) · verification tags ·
// provider grid (RomM MetadataTab).
function MetadataTab({ detail }: { detail: any }) {
  const providers = detail?.providers || {};
  const ordered = [...PROVIDERS].sort((a, b) => {
    const av = providers[a.key] ? 1 : 0, bv = providers[b.key] ? 1 : 0;
    return bv - av;
  });
  const hashes = detail?.hashes || {};
  const hashRows: [string, string | null][] = [
    ['CRC', hashes.crc], ['MD5', hashes.md5], ['SHA1', hashes.sha1], ['RA', hashes.ra],
  ];
  const copy = (v: string) => { try { navigator.clipboard?.writeText(v); toaster.toast({ title: 'Copied', body: v }); } catch { /* ignore */ } };
  const heading = (txt: string) => (
    <div style={{ fontSize: '13px', fontWeight: 600, color: V2.fg }}>{txt}</div>
  );
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      {/* File info */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {heading('File info')}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '12px 24px' }}>
          {[['Filename', detail?.fs_name || '—'], ['Size', detail?.fs_size_bytes ? fmtBytes(detail.fs_size_bytes) : '—']].map(([l, v]) => (
            <div key={l as string} style={{ display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0 }}>
              <div style={{ fontSize: '10.5px', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', color: V2.fgFaint }}>{l}</div>
              <div style={{ fontSize: '13px', color: V2.fg2, wordBreak: 'break-all' }}>{v}</div>
            </div>
          ))}
        </div>
      </div>
      {/* Hashes */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {heading('Hashes')}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' }}>
          {hashRows.map(([label, val]) => (
            <Focusable key={label} noFocusRing
              onActivate={() => val && copy(val)} onClick={() => val && copy(val)}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '4px 10px',
                background: V2.surface, border: `1px solid ${V2.borderStrong}`, borderRadius: V2.radiusChip,
                fontSize: '11.5px', cursor: val ? 'pointer' : 'default',
              }}>
              <span style={{ fontWeight: 700, color: V2.fgFaint, letterSpacing: '0.06em' }}>{label}</span>
              <span style={{ fontFamily: 'monospace', color: val ? V2.fg2 : V2.fgFaint }}>
                {val ? `${String(val).slice(0, 8)}…` : '—'}
              </span>
            </Focusable>
          ))}
        </div>
      </div>
      {/* Verification */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {heading('Verification')}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' }}>
          {(detail?.verifications || []).map((v: any) => (
            <span key={v.label} style={{
              display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '4px 10px',
              borderRadius: V2.radiusChip, fontSize: '11.5px', fontWeight: 600,
              background: v.match ? 'rgba(74,222,128,0.12)' : V2.surface,
              border: `1px solid ${v.match ? 'rgba(74,222,128,0.4)' : V2.borderStrong}`,
              color: v.match ? V2.success : V2.fgMuted,
            }}>{v.match ? <FaCheckCircle size={12} /> : <FaTimes size={12} />}{v.label}</span>
          ))}
        </div>
      </div>
      {/* Provider links */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {heading('Metadata sources')}
        <Focusable noFocusRing style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '10px' }}>
          {ordered.map((p) => <ProviderCard key={p.key} p={p} id={providers[p.key]} />)}
        </Focusable>
      </div>
    </div>
  );
}

// Module-level holders pass the selection between Game Browser routes without
// re-fetching (same pattern as _historyGameHolder).
let _libGroupHolder: { mode: string; group: LibGroup } | null = null;
// Sibling groups (same mode) so the game grid can page prev/next with L1/R1.
let _libGroupsHolder: { mode: string; groups: LibGroup[] } | null = null;
let _libGameHolder: LibGame | null = null;
// Route to return to when backing out of the game detail page. Steam's default
// NavigateBack on these custom Decky routes drops to the home/library root, so
// we navigate to the explicit origin route instead (set wherever a game opens).
let _libGameOrigin: string = "/romm-sync-library";
// Remember which library tab the user was on, so backing out of a games page
// returns to that tab (platforms/collections) instead of resetting to 'home'.
let _libLastTab: NavId = 'home';
// Per-group games cache (key: `${mode}:${groupKey}`) so paging to an already
// prefetched neighbour shows its covers instantly — the grid slides in with real
// content instead of popping in after an async fetch.
const _libGamesCache = new Map<string, LibGame[]>();

// Home tab data cache — survives tab-switch remounts so returning to Home paints
// instantly (then refreshes silently) instead of flashing "Loading…".
let _homeCache: {
  recent: LibGame[]; continuePlaying: LibGame[];
  platforms: LibGroup[]; collections: LibGroup[];
} | null = null;

// ---- Persistent browse cache -------------------------------------------------
// The Maps/vars above live in module scope, so they reset every time the plugin
// is reloaded or the Steam UI restarts — the browse lists then have to be
// refetched from the RomM server on the next open. Mirror them into localStorage
// (which survives reloads) so a restart paints from disk instantly and only
// silently refreshes in the background.
const _LS_LIB_PREFIX = 'romm:libcache:v1:';
const _LS_HOME_KEY = 'romm:homecache:v1';
const _LS_TTL_MS = 1000 * 60 * 60 * 24; // 24h; lists rarely churn, dots refresh on fetch
const _lsAvail = (() => { try { return typeof localStorage !== 'undefined'; } catch { return false; } })();

function _persistLibGroup(key: string, list: LibGame[]) {
  if (!_lsAvail) return;
  try { localStorage.setItem(_LS_LIB_PREFIX + key, JSON.stringify({ t: Date.now(), v: list })); } catch {}
}
function _dropLibGroup(key: string) {
  if (!_lsAvail) return;
  try { localStorage.removeItem(_LS_LIB_PREFIX + key); } catch {}
}
// Write-through helpers — use these instead of touching _libGamesCache directly.
function libCacheSet(key: string, list: LibGame[]) { _libGamesCache.set(key, list); _persistLibGroup(key, list); }
function libCacheDelete(key: string) { _libGamesCache.delete(key); _dropLibGroup(key); }
function persistHomeCache() {
  if (!_lsAvail || !_homeCache) return;
  try { localStorage.setItem(_LS_HOME_KEY, JSON.stringify({ t: Date.now(), v: _homeCache })); } catch {}
}

// Hydrate both caches once at module load from any non-expired localStorage data.
(function _hydrateBrowseCaches() {
  if (!_lsAvail) return;
  const now = Date.now();
  try {
    const stale: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith(_LS_LIB_PREFIX)) continue;
      try {
        const o = JSON.parse(localStorage.getItem(k) || 'null');
        if (o && Array.isArray(o.v) && (now - (o.t || 0)) < _LS_TTL_MS)
          _libGamesCache.set(k.slice(_LS_LIB_PREFIX.length), o.v);
        else stale.push(k);
      } catch { stale.push(k); }
    }
    stale.forEach((k) => { try { localStorage.removeItem(k); } catch {} });
  } catch {}
  try {
    const o = JSON.parse(localStorage.getItem(_LS_HOME_KEY) || 'null');
    if (o && o.v && (now - (o.t || 0)) < _LS_TTL_MS) _homeCache = o.v;
    else if (o) localStorage.removeItem(_LS_HOME_KEY);
  } catch {}
})();

const formatSpeed = (bytesPerSec: number): string => {
  if (bytesPerSec >= 1024 * 1024) return `${(bytesPerSec / (1024 * 1024)).toFixed(1)} MB/s`;
  if (bytesPerSec >= 1024) return `${(bytesPerSec / 1024).toFixed(0)} KB/s`;
  return `${bytesPerSec.toFixed(0)} B/s`;
};

// Background monitoring - runs independently of UI
let backgroundInterval: any = null;
const previousSyncStates = new Map<string, { sync_state: string, downloaded?: number, total?: number }>();

const checkForNotifications = async () => {
  try {
    const result = await getServiceStatus();

    // Collect all collections that need notifications
    const notificationsToShow: Array<{ name: string, downloaded: number, total: number, type: string }> = [];

    // Detect sync completion
    result.collections?.forEach((col: any) => {
      const previousData = previousSyncStates.get(col.name);
      const currentState = col.sync_state;

      // Log state for debugging
      // Detect transition to 'synced' from either 'syncing' OR 'not_synced'
      // 'syncing' -> 'synced': Normal case
      // 'not_synced' -> 'synced': Fast sync where we missed the 'syncing' state
      if (previousData && currentState === 'synced' &&
        (previousData.sync_state === 'syncing' || previousData.sync_state === 'not_synced')) {
        console.log(`[BACKGROUND NOTIFICATION] Collection '${col.name}' completed syncing: ${col.downloaded}/${col.total} ROMs (prev state: ${previousData.sync_state})`);
        notificationsToShow.push({
          name: col.name,
          downloaded: col.downloaded,
          total: col.total,
          type: 'sync'
        });
      }
      // Also detect when ROM count changes while in 'synced' state (deletions)
      else if (
        previousData?.sync_state === 'synced' &&
        currentState === 'synced' &&
        col.auto_sync &&
        previousData.downloaded !== undefined &&
        col.downloaded !== undefined &&
        previousData.downloaded !== col.downloaded
      ) {
        console.log(`[BACKGROUND NOTIFICATION] Collection '${col.name}' updated: ${col.downloaded}/${col.total} ROMs (was ${previousData.downloaded}/${previousData.total})`);
        notificationsToShow.push({
          name: col.name,
          downloaded: col.downloaded,
          total: col.total,
          type: 'update'
        });
      }

      // Update previous state and counts
      previousSyncStates.set(col.name, {
        sync_state: currentState,
        downloaded: col.downloaded,
        total: col.total
      });
    });

    // Show notifications with slight delay between each to prevent overlapping/deduplication
    for (let i = 0; i < notificationsToShow.length; i++) {
      const notification = notificationsToShow[i];
      // Add delay only if not the first notification
      if (i > 0) {
        await new Promise(resolve => setTimeout(resolve, 300));
      }
      console.log(`[BACKGROUND] Showing notification for ${notification.name}`);
      toaster.toast({
        title: `✅ ${notification.name} - Sync Complete`,
        body: `${notification.downloaded}/${notification.total} ROMs synced`,
        duration: 5000,
      });
    }

    if (notificationsToShow.length > 0) {
      console.log(`[BACKGROUND] Showed ${notificationsToShow.length} notification(s)`);
    }
  } catch (error) {
    console.error('[BACKGROUND NOTIFICATION] Error checking status:', error);
  }
};

const startBackgroundMonitoring = () => {
  if (backgroundInterval) {
    clearInterval(backgroundInterval);
  }
  console.log('[BACKGROUND] Starting background notification monitoring');
  backgroundInterval = setInterval(checkForNotifications, 2000);
};

const stopBackgroundMonitoring = () => {
  if (backgroundInterval) {
    console.log('[BACKGROUND] Stopping background notification monitoring');
    clearInterval(backgroundInterval);
    backgroundInterval = null;
  }
};

// Start monitoring immediately when module loads
console.log('[PLUGIN INIT] Module loaded, starting background monitoring');
startBackgroundMonitoring();

// Configuration / first-time setup page
function ConfigPage() {
  const [url, setUrl] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [romDir, setRomDir] = useState('');
  const [saveDir, setSaveDir] = useState('');
  const [biosDir, setBiosDir] = useState('');
  const [deviceName, setDeviceName] = useState('');
  const [deviceNameDefault, setDeviceNameDefault] = useState('SteamOS');
  const [hasPassword, setHasPassword] = useState(false);
  const [retrodeckDetected, setRetrodeckDetected] = useState(false);
  const [isFirstTime, setIsFirstTime] = useState(false);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pairCode, setPairCode] = useState('');
  const [pairing, setPairing] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const config = await getConfig();
        setUrl(config.url || '');
        setUsername(config.username || '');
        setRomDir(config.rom_directory || '');
        setSaveDir(config.save_directory || '');
        setBiosDir(config.bios_directory || '');
        setDeviceName(config.device_name || '');
        setDeviceNameDefault(config.device_name_default || 'SteamOS');
        setHasPassword(config.has_password || false);
        setRetrodeckDetected(config.retrodeck_detected || false);
        setIsFirstTime(!config.configured);
      } catch (e) {
        console.error('[ConfigPage] Failed to load config:', e);
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testRommConnection(url.trim(), username.trim(), password);
      setTestResult(result);
    } catch (e) {
      setTestResult({ success: false, message: 'Test failed unexpectedly.' });
    } finally {
      setTesting(false);
    }
  };

  const handlePair = async () => {
    if (!url.trim() || !pairCode.trim()) return;
    setPairing(true);
    try {
      const result = await pairDevice(url.trim(), pairCode.trim());
      if (result.success) {
        toaster.toast({ title: 'RomM Sync', body: 'Paired — connecting…', duration: 3000 });
        setPairCode('');
        Navigation.NavigateBack();
      } else {
        toaster.toast({ title: 'RomM Sync Error', body: result.message || 'Pairing failed.', duration: 5000 });
      }
    } catch (e) {
      toaster.toast({ title: 'RomM Sync Error', body: 'Pairing failed unexpectedly.', duration: 5000 });
    } finally {
      setPairing(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const effectiveDeviceName = deviceName.trim() || deviceNameDefault;
      const result = await saveConfig(url.trim(), username.trim(), password, romDir.trim(), saveDir.trim(), effectiveDeviceName, biosDir.trim());
      if (result.success) {
        toaster.toast({ title: 'RomM Sync', body: 'Settings saved — reconnecting...', duration: 3000 });
        Navigation.NavigateBack();
      } else {
        toaster.toast({ title: 'RomM Sync Error', body: result.error || 'Failed to save configuration.', duration: 5000 });
      }
    } catch (e) {
      toaster.toast({ title: 'RomM Sync Error', body: 'Failed to save configuration.', duration: 5000 });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return <div style={{ color: 'white', padding: '20px' }}>Loading configuration…</div>;
  }

  const canSubmit = url.trim().length > 0 && username.trim().length > 0 && (password.length > 0 || hasPassword);

  return (
    <div style={{ overflowY: 'auto', height: 'calc(100vh - 40px)', marginTop: '40px', paddingBottom: '40px', color: 'white' }}>

      {/* Header */}
      {isFirstTime ? (
        <div style={{ padding: '16px 16px 4px' }}>
          <div className={staticClasses.Title} style={{ marginBottom: '8px' }}>Welcome to RomM Sync</div>
          <div style={{ fontSize: '13px', color: '#d1d5db', lineHeight: '1.6' }}>
            Connect your SteamOS device to your RomM server to automatically sync ROMs and save files across devices.
          </div>
        </div>
      ) : (
        <div className={staticClasses.Title} style={{ margin: '0 16px 8px' }}>RomM Connection Setup</div>
      )}

      {/* RetroDECK banner */}
      {retrodeckDetected && (
        <div style={{
          margin: '8px 16px 4px',
          padding: '10px 14px',
          background: 'rgba(74, 222, 128, 0.12)',
          border: '1px solid rgba(74, 222, 128, 0.4)',
          borderRadius: '6px',
          fontSize: '13px',
          color: '#4ade80',
          lineHeight: '1.5',
        }}>
          <strong>RetroDECK detected!</strong> ROM and save directories have been pre-filled with RetroDECK defaults. You can change them below if needed.
        </div>
      )}

      <PanelSection title="Connection">
        <PanelSectionRow>
          <TextField
            label="RomM URL"
            value={url}
            onChange={(e: ChangeEvent<HTMLInputElement>) => { setUrl(e.target.value); setTestResult(null); }}
            description="e.g. https://romm.example.com"
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Username"
            value={username}
            onChange={(e: ChangeEvent<HTMLInputElement>) => { setUsername(e.target.value); setTestResult(null); }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Password"
            value={password}
            onChange={(e: ChangeEvent<HTMLInputElement>) => { setPassword(e.target.value); setTestResult(null); }}
            description={hasPassword && !password ? 'Leave blank to keep the saved password' : undefined}
            bIsPassword={true}
          />
        </PanelSectionRow>
        {testResult && (
          <PanelSectionRow>
            <div style={{ color: testResult.success ? '#4ade80' : '#f87171', fontSize: '0.9em', padding: '4px 0' }}>
              {testResult.success ? '✅' : '❌'} {testResult.message}
            </div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleTest} disabled={testing || saving || !url.trim() || !username.trim()}>
            {testing ? 'Testing…' : '🔌 Test Connection'}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Pair with code (recommended)">
        <PanelSectionRow>
          <TextField
            label="Pairing code"
            value={pairCode}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setPairCode(e.target.value)}
            description="Create a token in the RomM web UI, start pairing, and enter the code here — no password needed."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handlePair} disabled={pairing || saving || !url.trim() || !pairCode.trim()}>
            {pairing ? 'Pairing…' : '🔗 Pair Device'}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Device">
        <PanelSectionRow>
          <TextField
            label="Device Name"
            value={deviceName}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setDeviceName(e.target.value)}
            description={`Identifies this device in RomM for save syncing. Defaults to "${deviceNameDefault}" if left blank.`}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Directories">
        <PanelSectionRow>
          <TextField
            label="ROM Directory"
            value={romDir}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setRomDir(e.target.value)}
            description="Where ROMs will be downloaded"
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={async () => {
            try {
              const res = await openFilePicker(FileSelectionType.FOLDER, romDir || '/home/deck', false, true);
              if (res?.realpath) setRomDir(res.realpath);
            } catch { }
          }}>
            Browse…
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Save Directory"
            value={saveDir}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setSaveDir(e.target.value)}
            description="Where save files are stored (used for upload/download)"
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={async () => {
            try {
              const res = await openFilePicker(FileSelectionType.FOLDER, saveDir || '/home/deck', false, true);
              if (res?.realpath) setSaveDir(res.realpath);
            } catch { }
          }}>
            Browse…
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="BIOS Directory"
            value={biosDir}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setBiosDir(e.target.value)}
            description="Where BIOS/firmware files are stored"
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={async () => {
            try {
              const res = await openFilePicker(FileSelectionType.FOLDER, biosDir || '/home/deck', false, true);
              if (res?.realpath) setBiosDir(res.realpath);
            } catch { }
          }}>
            Browse…
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleSave} disabled={saving || testing || !canSubmit}>
            {saving ? 'Saving…' : isFirstTime ? '🚀 Connect & Start' : '💾 Save & Apply'}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={async () => {
            try {
              const r = await clearCoverCache();
              _coverCache.clear(); _coverInflight.clear(); // drop the frontend hot layer too
              toaster.toast({ title: 'Cover cache cleared', body: r?.success ? `${r.removed ?? 0} files removed` : (r?.message || 'Error') });
            } catch (e) { toaster.toast({ title: 'Clear failed', body: String(e) }); }
          }}>
            Clear cover cache
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => Navigation.NavigateBack()}>
            Cancel
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </div>
  );
}

// Settings page component
// ---------------------------------------------------------------------------
// Shared save/state version types + formatters, used by the game detail
// Save Data tab (server save/state versions; restore in-place or as a copy).
// ---------------------------------------------------------------------------

interface HistoryEntry {
  id: number; slot: any; save_type: string; file_name: string;
  updated_at: string | null; size_bytes: number | null;
  device: string | null; has_screenshot: boolean;
}

function fmtHistTs(iso: string | null): string {
  if (!iso) return "Unknown time";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

function fmtHistSize(n: number | null): string {
  if (n == null || isNaN(n as any)) return "";
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB']) {
    if (v < 1024) return u === 'B' ? `${v.toFixed(0)} ${u}` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v.toFixed(1)} TB`;
}

function slotLabel(e: HistoryEntry): string {
  const fn = (e.file_name || '').toLowerCase();
  if (fn.endsWith('.state.auto')) return 'Auto';
  const m = fn.match(/\.state(\d+)$/);
  if (m) return `Slot ${m[1]}`;
  if (fn.endsWith('.state')) return 'Quicksave';
  if (e.slot != null && e.slot !== '') return String(e.slot);
  return e.save_type === 'states' ? 'State' : 'Save';
}

// ---------------------------------------------------------------------------
// Game Browser — controller-first library with cover art, per-game download,
// and metadata. Styled with RomM v2 tokens (V2), not the Steam/Decky chrome.
// Routes: /romm-sync-library  ->  /romm-sync-library/:key  ->  /romm-sync-game/:romId
// ---------------------------------------------------------------------------

// Suppress Steam's gamepad focus highlight (the square "overdraw" box drawn on
// every Focusable) inside the plugin UI. We rely on our own per-component focus
// styling (card scale/glow, row lift, button glow) instead, so the Steam box is
// fully removed rather than restyled.
// Steam draws its gamepad focus box in JS; we disable it per-element via the
// noFocusRing prop on every Focusable. Here we only strip the CEF :focus
// outline (our components supply their own glow), without touching box-shadow
// so our focus glows survive.
const V2_FOCUS_STYLE = `
  .romm-ui *:focus, .romm-ui *:focus-visible { outline: none !important; }
`;

// Shared list-row / tile hover+focus treatment — the canonical RomM interaction
// vocabulary (brand ring + soft purple glow + slight lift). Injected globally by
// v2Page so every list (saves, achievements, …) reads identically instead of
// each component reinventing it. `.romm-row` for full-width rows, `.romm-tile`
// for the card grid. Note: hosts must NOT clip these (no overflow:hidden on the
// immediate wrapper) or the outset glow gets cut off.
const _EASE = 'cubic-bezier(0.22,1,0.36,1)';
const V2_ROW_STYLE = `
  .romm-row { background: ${V2.surface}; border: 1px solid ${V2.border}; transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ${_EASE}, box-shadow 0.15s ease; }
  .romm-row:hover, .romm-row:focus-within {
    background: ${V2.surfaceHover}; border-color: ${V2.brand}; transform: translateY(-1px);
    box-shadow: 0 0 0 2px ${V2.brand}, 0 0 16px rgba(139,116,232,0.45);
  }
  .romm-tile { background: ${V2.surface}; border: 1px solid ${V2.border}; transition: background 0.15s ease, transform 0.15s ${_EASE}, box-shadow 0.15s ease, border-color 0.15s ease; }
  .romm-tile:hover, .romm-tile:focus-within {
    background: ${V2.surfaceHover}; transform: translateY(-2px); border-color: ${V2.brand};
    box-shadow: 0 8px 22px rgba(0,0,0,0.4), 0 0 0 2px ${V2.brand}, 0 0 16px rgba(139,116,232,0.45);
  }
`;

function v2Page(children: any, bgUri: string | null = null) {
  return (
    <div className="romm-ui" style={{
      fontFamily: V2.font, color: V2.fg, background: V2.bg,
      position: 'relative', overflowY: 'auto', height: 'calc(100vh - 40px)',
      marginTop: '40px', scrollPaddingTop: '120px',
    }}>
      <style>{V2_FOCUS_STYLE}{V2_ROW_STYLE}</style>
      <V2Bg uri={bgUri} />
      <div style={{ position: 'relative', zIndex: 2, padding: '0 0 40px' }}>
        {children}
      </div>
    </div>
  );
}

const NAV_ORDER: NavId[] = ['home', 'platforms', 'collections', 'search'];

// RomM RTextField (filled) search field — uses @decky/ui TextField for
// Steam virtual keyboard support, with CSS overrides to strip the default
// Steam DialogInput styling and apply the V2 theme. Wrapped in Focusable for
// gamepad navigation; onActivate focuses the inner input to trigger keyboard.
const V2SearchField = forwardRef(function V2SearchField(
  { value, onChange }: { value: string; onChange: (v: string) => void },
  fwdRef: Ref<any>,
) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [focused, setFocused] = useState(false);

  return (
    <Focusable
      ref={(el: any) => {
        wrapperRef.current = el;
        if (typeof fwdRef === 'function') fwdRef(el);
        else if (fwdRef) (fwdRef as any).current = el;
      }}
      noFocusRing
      onActivate={() => {
        const input = wrapperRef.current?.querySelector('input');
        if (input) input.focus();
      }}
      style={{ borderRadius: V2.radiusMd }}
    >
      <style>{`
        .romm-search-wrap label { display: none !important; }
        .romm-search-wrap > div { background: transparent !important; border: none !important; box-shadow: none !important; padding: 0 !important; margin: 0 !important; }
        .romm-search-wrap > div > div { background: transparent !important; border: none !important; box-shadow: none !important; padding: 0 !important; }
        .romm-search-wrap input { background: transparent !important; border: none !important; outline: none !important; box-shadow: none !important; color: ${V2.fg} !important; font-size: 14px !important; font-family: inherit !important; padding: 0 !important; margin: 0 !important; height: auto !important; min-height: 0 !important; caret-color: ${V2.brand} !important; }
        .romm-search-wrap input::placeholder { color: rgba(255,255,255,0.45) !important; }
      `}</style>
      <div className="romm-search-wrap" style={{
        display: 'flex', alignItems: 'center', gap: '10px', height: '40px', padding: '0 12px',
        borderRadius: V2.radiusMd,
        background: focused ? V2.surfaceHover : 'rgba(255,255,255,0.045)',
        border: `1px solid ${focused ? V2.brand : V2.border}`,
        boxShadow: focused ? `0 0 0 3px rgba(139,116,232,0.22)` : 'none',
        transition: 'background 0.2s, border-color 0.2s, box-shadow 0.2s',
      }}>
        <FaSearch size={14} color={focused ? V2.brandHover : V2.fgMuted} style={{ flexShrink: 0 }} />
        <div style={{ flex: '1 1 auto', minWidth: 0 }}
          onFocusCapture={() => setFocused(true)}
          onBlurCapture={() => setFocused(false)}
        >
          <TextField
            value={value}
            onChange={(e: ChangeEvent<HTMLInputElement>) => onChange(e.target.value)}
          />
        </div>
        {value && (
          <div onClick={() => onChange('')}
            style={{
              flexShrink: 0, cursor: 'pointer', color: V2.fgMuted, display: 'flex', alignItems: 'center',
              padding: '2px', borderRadius: '50%',
            }}>
            <FaTimes size={13} />
          </div>
        )}
      </div>
    </Focusable>
  );
});

// V2TextField — labeled text input sharing the V2SearchField look (RomM
// RTextField filled variant): rounded surface box, brand focus border + halo.
// Used by the setup wizard so its fields match the library search bar.
function V2TextField({ label, value, onChange, password, placeholder, icon, mono, maxLength }:
  { label?: string; value: string; onChange: (v: string) => void; password?: boolean; placeholder?: string; icon?: any; mono?: boolean; maxLength?: number }) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [focused, setFocused] = useState(false);
  const uid = useRef(`v2tf-${Math.random().toString(36).slice(2, 8)}`).current;
  useEffect(() => {
    if (placeholder) {
      const input = wrapperRef.current?.querySelector('input');
      if (input) input.setAttribute('placeholder', placeholder);
    }
  }, [placeholder]);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', width: '100%' }}>
      {label && <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: V2.fgMuted, textAlign: 'center' }}>{label}</div>}
      <style>{`.${uid} label{display:none!important}.${uid}>div{background:transparent!important;border:none!important;box-shadow:none!important;padding:0!important;margin:0!important}.${uid}>div>div{background:transparent!important;border:none!important;box-shadow:none!important;padding:0!important}.${uid} input{background:transparent!important;border:none!important;outline:none!important;box-shadow:none!important;color:${V2.fg}!important;font-size:${mono?'20px':'14px'}!important;font-weight:${mono?'700':'400'}!important;font-family:${mono?'monospace':'inherit'}!important;padding:0!important;margin:0!important;height:auto!important;min-height:0!important;caret-color:${V2.brand}!important;text-align:center!important;letter-spacing:${mono?'.3em':'normal'}!important;text-indent:${mono?'.3em':0}!important;text-transform:${mono?'uppercase':'none'}!important}.${uid} input::placeholder{color:rgba(255,255,255,0.40)!important}`}</style>
      <div ref={wrapperRef} className={uid} style={{
        display: 'flex', alignItems: 'center', gap: '10px', height: '40px', padding: '0 12px',
        borderRadius: V2.radiusMd,
        background: focused ? V2.surfaceHover : 'rgba(255,255,255,0.045)',
        border: `1px solid ${focused ? V2.brand : V2.border}`,
        boxShadow: focused ? `0 0 0 3px rgba(139,116,232,0.22)` : 'none',
        transition: 'background 0.2s, border-color 0.2s, box-shadow 0.2s',
      }}>
        {icon && <span style={{ flexShrink: 0, color: focused ? V2.brandHover : V2.fgMuted, display: 'inline-flex' }}>{icon}</span>}
        <div style={{ flex: '1 1 auto', minWidth: 0 }}
          onFocusCapture={() => setFocused(true)}
          onBlurCapture={() => setFocused(false)}
        >
          <TextField
            value={value}
            bIsPassword={password}
            onChange={(e: ChangeEvent<HTMLInputElement>) => onChange(maxLength != null ? e.target.value.slice(0, maxLength) : e.target.value)}
          />
        </div>
      </div>
    </div>
  );
}

// PairCodeField — masked XXXX-XXXX entry. Shows an 8-slot template where each
// typed character replaces one `*` (rather than a placeholder that vanishes on
// the first keystroke). A transparent, char-aligned input sits over the mask so
// the caret lands on the next empty slot.
function PairCodeField({ label, value, onChange }:
  { label?: string; value: string; onChange: (v: string) => void }) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [focused, setFocused] = useState(false);
  const digits = value.replace(/-/g, '');
  const font: any = { fontFamily: 'monospace', fontSize: '20px', fontWeight: 700, letterSpacing: '0.3em', textIndent: '0.3em' };
  const cells: any[] = [];
  for (let i = 0; i < 8; i++) {
    const ch = digits[i];
    cells.push(<span key={i} style={{ color: ch ? V2.fg : V2.fgMuted }}>{ch || '*'}</span>);
    if (i === 3) cells.push(<span key="dash" style={{ color: digits.length > 4 ? V2.fg : V2.fgMuted }}>-</span>);
  }
  const uid = useRef(`v2pf-${Math.random().toString(36).slice(2, 8)}`).current;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', width: '100%' }}>
      {label && <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: V2.fgMuted, textAlign: 'center' }}>{label}</div>}
      <style>{`.${uid} label{display:none!important}.${uid}>div{background:transparent!important;border:none!important;box-shadow:none!important;padding:0!important;margin:0!important}.${uid}>div>div{background:transparent!important;border:none!important;box-shadow:none!important;padding:0!important}.${uid} input{position:absolute!important;inset:0!important;width:100%!important;background:transparent!important;border:none!important;outline:none!important;box-shadow:none!important;color:transparent!important;caret-color:${V2.brand}!important;padding:0!important;margin:0!important;height:100%!important;min-height:0!important;font-family:monospace!important;font-size:20px!important;font-weight:700!important;letter-spacing:.3em!important;text-indent:.3em!important;text-transform:uppercase!important}`}</style>
      <div ref={wrapperRef} className={uid} style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center', height: '40px', padding: '0 12px',
        borderRadius: V2.radiusMd,
        background: focused ? V2.surfaceHover : 'rgba(255,255,255,0.045)',
        border: `1px solid ${focused ? V2.brand : V2.border}`,
        boxShadow: focused ? `0 0 0 3px rgba(139,116,232,0.22)` : 'none',
        transition: 'background 0.2s, border-color 0.2s, box-shadow 0.2s',
      }}>
        <div style={{ position: 'relative', display: 'inline-block', width: '100%' }}
          onFocusCapture={() => setFocused(true)}
          onBlurCapture={() => setFocused(false)}
        >
          <div style={{ ...font, whiteSpace: 'pre', pointerEvents: 'none' }}>{cells}</div>
          <TextField
            value={value}
            onChange={(e: ChangeEvent<HTMLInputElement>) => onChange(e.target.value.slice(0, 9))}
          />
        </div>
      </div>
    </div>
  );
}

// Search tab — debounced text filter over the whole library, results as a
// cover-art grid (the nav 'Search' destination).
function SearchPanel({ onOpen, onBg }: { onOpen: (g: LibGame) => void; onBg: (uri: string | null) => void }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState<LibGame[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    // Enter the loading state synchronously so the debounce window shows
    // "Searching…" rather than briefly flashing the "no match" empty state.
    setLoading(true);
    // Empty query browses the whole library (mirrors RomM's Search default),
    // so it resolves instantly with no debounce; typed queries debounce.
    const delay = q.trim() ? 250 : 0;
    const t = setTimeout(async () => {
      try { const r = await searchGames(q); setResults(r?.success ? (r.games || []) : []); }
      catch { setResults([]); }
      finally { setLoading(false); }
    }, delay);
    return () => clearTimeout(t);
  }, [q]);
  return (
    <div style={{ padding: '0 16px' }}>
      <div style={{ maxWidth: '520px', margin: '0 auto 16px' }}>
        <V2SearchField value={q} onChange={setQ} />
      </div>
      {loading ? (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px', textAlign: 'center' }}>
          {q.trim() ? 'Searching…' : 'Loading library…'}
        </div>
      ) : results.length === 0 ? (
        <div style={{ padding: '24px', color: V2.fgMuted, fontSize: '13px', textAlign: 'center' }}>
          {q.trim() ? `No games match "${q.trim()}".` : 'No games in your library yet.'}
        </div>
      ) : (
        <Focusable noFocusRing style={{
          display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))',
          gap: '18px 16px', padding: '6px 0',
        }}>
          {results.map((g) => <GameTile key={g.rom_id} game={g} onOpen={onOpen} onActiveCover={onBg} />)}
        </Focusable>
      )}
    </div>
  );
}

// Small count tag — RomM RTag x-small used in CardRow headers.
function Tag({ children }: { children: any }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
      minWidth: '18px', height: '18px', padding: '0 6px', borderRadius: V2.radiusChip,
      background: V2.surface, border: `1px solid ${V2.border}`,
      fontSize: '11px', fontWeight: 700, color: V2.fg2, fontVariantNumeric: 'tabular-nums',
    }}>{children}</span>
  );
}

// CardRow — RomM v2 Home section: header (icon + title + count) over a
// horizontal-scroll track. Gamepad focus scrolls the track natively; the
// gradient chevron arrows (RomM style) appear only when the track overflows
// in that direction, signaling "more to the right".
function CardRow({ icon, title, count, children }:
  { icon: any; title: string; count?: number; children: any }) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [canLeft, setCanLeft] = useState(false);
  const [canRight, setCanRight] = useState(false);

  const update = () => {
    const el = trackRef.current;
    if (!el) return;
    setCanLeft(el.scrollLeft > 8);
    setCanRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 8);
  };
  useEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    requestAnimationFrame(update);
    const ro = new ResizeObserver(update);
    ro.observe(el);
    for (const c of Array.from(el.children)) ro.observe(c as Element);
    return () => ro.disconnect();
  }, [children]);
  const scroll = (dir: -1 | 1) => {
    const el = trackRef.current;
    if (!el) return;
    el.scrollBy({ left: dir * el.clientWidth * 0.8, behavior: 'smooth' });
  };

  // Arrows are always mounted and fade in/out (opacity + slide) with the
  // overflow state so they don't pop; hidden ones drop pointer events.
  const arrow = (dir: -1 | 1, show: boolean): any => ({
    position: 'absolute', top: '50%', zIndex: 10,
    [dir < 0 ? 'left' : 'right']: '8px',
    width: '36px', height: '36px', borderRadius: '50%',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    cursor: show ? 'pointer' : 'default', pointerEvents: show ? 'auto' : 'none',
    background: 'rgba(0,0,0,0.4)', color: V2.fg2, border: `1px solid ${V2.border}`,
    backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)',
    boxShadow: '0 2px 10px rgba(0,0,0,0.4)',
    opacity: show ? 1 : 0,
    transform: `translateY(-50%) translateX(${show ? '0' : `${dir < 0 ? -6 : 6}px`})`,
    transition: 'opacity 0.2s ease, transform 0.2s ease',
  });

  return (
    <section style={{ marginBottom: '22px' }}>
      <header style={{
        display: 'flex', alignItems: 'center', gap: '10px',
        padding: '0 16px', marginBottom: '12px', color: V2.fg2,
      }}>
        <span style={{ opacity: 0.6, display: 'inline-flex', alignItems: 'center' }}>{icon}</span>
        <h2 style={{ fontSize: '14.5px', fontWeight: 600, letterSpacing: '0.01em', lineHeight: 1.2, margin: 0 }}>{title}</h2>
        {count != null && <Tag>{count}</Tag>}
      </header>
      <div style={{ position: 'relative' }}>
        <div style={arrow(-1, canLeft)} onClick={() => canLeft && scroll(-1)}><FaChevronLeft size={15} /></div>
        <div style={arrow(1, canRight)} onClick={() => canRight && scroll(1)}><FaChevronRight size={15} /></div>
        {/* Top/bottom padding gives the focus scale + glow room so the
            scroll container (overflow-x:auto clips y too) doesn't crop the
            top of hovered covers. Matches RomM's 16/20 track padding. */}
        <div
          ref={trackRef}
          onScroll={update}
          style={{ overflowX: 'auto', overflowY: 'visible' }}
        >
          <Focusable noFocusRing flow-children="horizontal" style={{ display: 'flex', gap: '12px', padding: '24px 16px 28px' }}>
            {children}
          </Focusable>
        </div>
      </div>
    </section>
  );
}

// Home dashboard — faithful to RomM v2 Home.vue: horizontal CardRows
// (Continue playing / Recently added / Platforms / Collections).
function HomePanel({ onOpen, onOpenGroup, onBg }:
  { onOpen: (g: LibGame) => void; onOpenGroup: (mode: string, g: LibGroup, gs: LibGroup[]) => void; onBg: (uri: string | null) => void }) {
  // Seed from the module-level cache so re-mounting (tab switch back to Home)
  // paints instantly instead of flashing "Loading…" and refetching.
  const c0 = _homeCache;
  const [recent, setRecent] = useState<LibGame[]>(c0?.recent || []);
  const [continuePlaying, setContinuePlaying] = useState<LibGame[]>(c0?.continuePlaying || []);
  const [platforms, setPlatforms] = useState<LibGroup[]>(c0?.platforms || []);
  const [collections, setCollections] = useState<LibGroup[]>(c0?.collections || []);
  const [loading, setLoading] = useState(!c0);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [h, p, c] = await Promise.all([
          getHomeData(), getLibraryGroups('platform'), getLibraryGroups('collection'),
        ]);
        if (!alive) return;
        const next = { ..._homeCache } as NonNullable<typeof _homeCache>;
        if (h?.success) { setRecent(h.recent || []); setContinuePlaying(h.continue_playing || []); next.recent = h.recent || []; next.continuePlaying = h.continue_playing || []; }
        if (p?.success) { setPlatforms(p.groups || []); next.platforms = p.groups || []; }
        if (c?.success) { setCollections(c.groups || []); next.collections = c.groups || []; }
        _homeCache = next;
        persistHomeCache();
      } catch (e) { console.error('home load failed', e); }
      finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; };
  }, []);

  // Land focus on the first card of whichever row renders first, so the user can
  // navigate straight in (no DOWN press needed off the nav bar).
  const firstList = continuePlaying.length ? 'cp' : recent.length ? 'rc'
    : platforms.length ? 'pf' : collections.length ? 'cl' : null;
  const firstRef = useAutoFocus(!loading && firstList !== null, firstList);

  if (loading) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        minHeight: '60vh', color: V2.fgMuted, fontSize: '13px',
      }}>Loading…</div>
    );
  }
  return (
    <div>
      {/* Continue playing — per-user last_played from RomM (cross-device). */}
      {continuePlaying.length > 0 && (
        <CardRow icon={<FaPlay size={14} />} title="Continue playing" count={continuePlaying.length}>
          {continuePlaying.map((g, i) => (
            // Screenshot cards size to their natural (landscape) width; games
            // with no screenshot fall back to the portrait 132px cover.
            <div key={g.rom_id} style={{ flexShrink: 0, ...(g.screenshot ? {} : { width: '132px' }) }}>
              <GameTile game={g} onOpen={onOpen} onActiveCover={onBg}
                focusRef={firstList === 'cp' && i === 0 ? firstRef : undefined} />
            </div>
          ))}
        </CardRow>
      )}

      {/* Recently added */}
      {recent.length > 0 && (
        <CardRow icon={<FaRegClock size={16} />} title="Recently added" count={recent.length}>
          {recent.map((g, i) => (
            <div key={g.rom_id} style={{ width: '132px', flexShrink: 0 }}>
              <GameTile game={g} onOpen={onOpen} onActiveCover={onBg}
                focusRef={firstList === 'rc' && i === 0 ? firstRef : undefined} />
            </div>
          ))}
        </CardRow>
      )}

      {/* Platforms */}
      {platforms.length > 0 && (
        <CardRow icon={<FaGamepad size={16} />} title="Platforms" count={platforms.length}>
          {platforms.map((g, i) => (
            <div key={g.key} style={{ width: '150px', flexShrink: 0 }}>
              <PlatformTile group={g} onOpen={(grp) => onOpenGroup('platform', grp, platforms)}
                focusRef={firstList === 'pf' && i === 0 ? firstRef : undefined} />
            </div>
          ))}
        </CardRow>
      )}

      {/* Collections */}
      {collections.length > 0 && (
        <CardRow icon={<FaLayerGroup size={15} />} title="Collections" count={collections.length}>
          {collections.map((g, i) => (
            <div key={g.key} style={{ width: '132px', flexShrink: 0 }}>
              <CollectionTile group={g} onOpen={(grp) => onOpenGroup('collection', grp, collections)}
                focusRef={firstList === 'cl' && i === 0 ? firstRef : undefined} />
            </div>
          ))}
        </CardRow>
      )}

      {continuePlaying.length === 0 && recent.length === 0 && platforms.length === 0 && collections.length === 0 && (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px' }}>No games in your library yet.</div>
      )}
    </div>
  );
}

function LibraryGroupsPage() {
  const [active, setActive] = useState<NavId>(_libLastTab);
  const [groups, setGroups] = useState<LibGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [bgUri, setBgUri] = useState<string | null>(null);
  const mode = active === 'collections' ? 'collection' : 'platform';

  const load = async (m: string) => {
    setLoading(true);
    try {
      const res = await getLibraryGroups(m);
      setGroups(res?.success ? (res.groups || []) : []);
    } catch (e) {
      console.error('get_library_groups failed', e);
      setGroups([]);
    } finally {
      setLoading(false);
    }
  };

  // Only platforms/collections fetch groups; home/search are placeholders.
  useEffect(() => {
    if (active === 'platforms' || active === 'collections') load(mode);
  }, [active]);

  // Focus the first platform/collection tile once the index grid loads.
  const isGroupTab = active === 'platforms' || active === 'collections';
  const firstGroupRef = useAutoFocus(isGroupTab && !loading && groups.length > 0, active);

  const openGroup = (g: LibGroup) => {
    _libGroupHolder = { mode, group: g };
    _libGroupsHolder = { mode, groups };
    Navigation.Navigate(`/romm-sync-library/${encodeURIComponent(g.key)}`);
  };

  // Home rows carry their own mode + sibling list (independent of the active tab).
  const openGroupFrom = (m: string, g: LibGroup, gs: LibGroup[]) => {
    _libGroupHolder = { mode: m, group: g };
    _libGroupsHolder = { mode: m, groups: gs };
    Navigation.Navigate(`/romm-sync-library/${encodeURIComponent(g.key)}`);
  };

  const openGame = (g: LibGame) => {
    _libGameHolder = g;
    // Opened from the home/search/index grid → back returns to the library root.
    _libGameOrigin = "/romm-sync-library";
    Navigation.Navigate(`/romm-sync-game/${g.rom_id}`);
  };

  const onTab = (id: NavId) => { _libLastTab = id; setActive(id); };

  // L1 / R1 page through the nav tabs (BackgroundArt cleared on home/search).
  // After switching, the active panel remounts and drops gamepad focus, so we
  // re-anchor focus on the persistent active nav pill — otherwise Steam eats the
  // next bumper press to re-acquire focus (the "press twice to switch" bug).
  const navPillRef = useRef<any>(null);
  const cycle = (dir: -1 | 1) => {
    const i = NAV_ORDER.indexOf(active);
    const next = NAV_ORDER[(i + dir + NAV_ORDER.length) % NAV_ORDER.length];
    _libLastTab = next;
    setActive(next);
    requestAnimationFrame(() => { try { navPillRef.current?.focus(); } catch { } });
  };
  const onButtonDown = (evt: any) => {
    const b = evt?.detail?.button;
    if (b === GamepadButton.BUMPER_LEFT) cycle(-1);
    else if (b === GamepadButton.BUMPER_RIGHT) cycle(1);
    else if (b === GamepadButton.SELECT) Navigation.Navigate("/romm-sync-settings");
  };

  return v2Page(
    <Focusable noFocusRing onButtonDown={onButtonDown}>
      <V2NavBar active={active} onTab={onTab} activeRef={navPillRef} />

      <div style={{ height: '8px' }} />

      {active === 'home' ? (
        <HomePanel onOpen={openGame} onOpenGroup={openGroupFrom} onBg={setBgUri} />
      ) : active === 'search' ? (
        <SearchPanel onOpen={openGame} onBg={setBgUri} />
      ) : loading ? (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px' }}>Loading…</div>
      ) : groups.length === 0 ? (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px' }}>
          {mode === 'collection' ? 'No collections found on the server.' : 'No games found.'}
        </div>
      ) : mode === 'platform' ? (
        <Focusable noFocusRing
          style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
            gap: '14px', padding: '0 16px',
          }}
        >
          {groups.map((g, i) => <PlatformTile key={g.key} group={g} onOpen={openGroup} focusRef={i === 0 ? firstGroupRef : undefined} />)}
        </Focusable>
      ) : (
        // Collections, grouped into sections (Collections / Smart / Virtual) like
        // RomM's collection index.
        (() => {
          const sections: { title: string; kinds: string[] }[] = [
            { title: 'Collections', kinds: ['favorite', 'collection'] },
            { title: 'Smart', kinds: ['smart'] },
            { title: 'Virtual', kinds: ['virtual'] },
          ];
          // Key of the very first rendered tile across all sections (gets focus).
          let firstKey: string | null = null;
          for (const s of sections) {
            const it = groups.find((g) => s.kinds.includes(g.kind || 'collection'));
            if (it) { firstKey = it.key; break; }
          }
          return sections.map((s) => {
            const items = groups.filter((g) => s.kinds.includes(g.kind || 'collection'));
            if (items.length === 0) return null;
            return (
              <div key={s.title} style={{ marginBottom: '6px' }}>
                <div style={{
                  fontSize: '11px', fontWeight: 700, letterSpacing: '0.06em',
                  textTransform: 'uppercase', color: V2.fgMuted, padding: '6px 16px 10px',
                  display: 'flex', alignItems: 'center', gap: '6px',
                }}>
                  <FaBookmark size={10} /><span>{s.title}</span>
                </div>
                <Focusable noFocusRing
                  style={{
                    display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))',
                    gap: '18px 16px', padding: '0 16px 8px',
                  }}
                >
                  {items.map((g) => <CollectionTile key={g.key} group={g} onOpen={openGroup} focusRef={g.key === firstKey ? firstGroupRef : undefined} />)}
                </Focusable>
              </div>
            );
          });
        })()
      )}
    </Focusable>,
    bgUri,
  );
}

function LibraryGamesPage() {
  const holder = _libGroupHolder;
  const mode = holder?.mode || 'platform';
  const [group, setGroup] = useState<LibGroup | null>(holder?.group || null);
  const cacheKey = (k: string) => `${mode}:${k}`;
  const cached0 = group ? _libGamesCache.get(cacheKey(group.key)) : undefined;
  const [games, setGames] = useState<LibGame[]>(cached0 || []);
  const [loading, setLoading] = useState(!cached0);
  const [bgUri, setBgUri] = useState<string | null>(null);

  // Sibling groups (same mode) so the game grid can page prev/next with L1/R1.
  const siblings = (_libGroupsHolder && _libGroupsHolder.mode === mode) ? _libGroupsHolder.groups : [];

  // Warm the cover art for a list of games (capped — just the first screenful is
  // enough to make the slide land painted; the rest stream in as usual).
  const warmCovers = (gs: LibGame[]) => {
    for (const g of gs.slice(0, 18)) {
      if (g.screenshot) awaitCover(`img:${g.screenshot}`, () => qGetImage(g.screenshot!));
      else if (g.has_cover) awaitCover(`cover:${g.rom_id}:false`, () => qGetGameCover(g.rom_id, false));
    }
  };

  // Fetch one group's games (and warm their covers) into the cache.
  const prefetch = async (key: string) => {
    const ck = cacheKey(key);
    let gs = _libGamesCache.get(ck);
    if (!gs) {
      try {
        const res = await getLibraryGames(mode, key);
        const list: LibGame[] | null = res?.success ? (res.games || []) : null;
        if (list) { gs = list; libCacheSet(ck, list); }
      } catch { /* best-effort prefetch */ }
    }
    if (gs) warmCovers(gs);
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      if (!group) { setLoading(false); return; }
      setBgUri(null);
      const ck = cacheKey(group.key);
      const hit = _libGamesCache.get(ck);
      if (hit) {
        // Instant: real covers slide in with the carousel, no pop-in.
        setGames(hit); setLoading(false);
      } else {
        setLoading(true);
        try {
          const res = await getLibraryGames(mode, group.key);
          if (res?.success) libCacheSet(ck, res.games || []);
          if (alive) setGames(res?.success ? (res.games || []) : []);
        } catch (e) {
          console.error('get_library_games failed', e);
          if (alive) setGames([]);
        } finally {
          if (alive) setLoading(false);
        }
      }
      // Warm the immediate neighbours so the next L1/R1 is instant.
      const i = siblings.findIndex((s) => s.key === group.key);
      if (i >= 0) {
        const nbrs = [siblings[i - 1], siblings[i + 1],
          siblings[(i + 1) % siblings.length], siblings[(i - 1 + siblings.length) % siblings.length]];
        for (const n of nbrs) if (n) prefetch(n.key);
      }
    })();
    return () => { alive = false; };
  }, [group?.key]);

  // Once games load (initial entry or after L1/R1 paging), drop focus onto the
  // first tile so the user can navigate straight into the grid — no extra DOWN
  // press to leave the header carousel.
  const firstTileRef = useAutoFocus(!loading && games.length > 0, group?.key);

  const openGame = (g: LibGame) => {
    _libGameHolder = g;
    // Return to THIS collection/platform's games page when backing out.
    _libGameOrigin = group ? `/romm-sync-library/${encodeURIComponent(group.key)}` : "/romm-sync-library";
    Navigation.Navigate(`/romm-sync-game/${g.rom_id}`);
  };

  // L1 / R1 page through sibling groups (same mode) without backing out.
  // (`siblings` is declared above for the prefetch logic.)
  // Re-anchor focus on the persistent header row after paging — the games grid
  // remounts on group change and drops gamepad focus, which would otherwise make
  // Steam eat the next bumper press ("press twice to switch").
  const headerRef = useRef<any>(null);
  // The selected slot grows to fit its full label (no truncation); measure its
  // real width so the track translate keeps it perfectly centred.
  const selSlotRef = useRef<HTMLDivElement | null>(null);
  const [selW, setSelW] = useState(120);
  // Direction of the last group change, so the games grid slides in from the same
  // side as the carousel — making the header + covers read as one moving surface.
  const [slideDir, setSlideDir] = useState<1 | -1>(1);
  const cycle = (dir: -1 | 1) => {
    if (!group || siblings.length < 2) return;
    const i = siblings.findIndex((s) => s.key === group.key);
    if (i < 0) return;
    const next = siblings[(i + dir + siblings.length) % siblings.length];
    setSlideDir(dir);
    _libGroupHolder = { mode, group: next };
    setGroup(next);
    requestAnimationFrame(() => { try { headerRef.current?.focus(); } catch { } });
  };
  // Collection auto-sync toggle (Y). Keyed by collection name (== group.key).
  // Optimistic local override layered over the backend's `synced` flag so the
  // header reflects the change instantly; backend persists it to settings.ini.
  const isCollection = mode === 'collection';
  // Virtual collections are browse/download only (no auto-sync, no local-delete).
  const isVirtual = !!group?.virtual;
  const [syncOverrides, setSyncOverrides] = useState<Record<string, boolean>>({});
  const isSynced = isCollection && !isVirtual && group
    ? (syncOverrides[group.key] ?? !!group.synced) : false;
  const toggleSync = async () => {
    if (!isCollection || isVirtual || !group) return;
    const name = group.key;
    const next = !(syncOverrides[name] ?? !!group.synced);
    setSyncOverrides((m) => ({ ...m, [name]: next }));
    try {
      const ok = await toggleCollectionSync(name, next);
      if (ok === false) throw new Error('backend declined');
      toaster.toast({ title: next ? 'Auto-sync on' : 'Auto-sync off', body: name });
    } catch (e) {
      setSyncOverrides((m) => ({ ...m, [name]: !next })); // revert
      toaster.toast({ title: 'Sync toggle failed', body: String(e) });
    }
  };

  // One-shot "Sync missing": download every game in this collection that isn't
  // already local, through the per-game download path (covers light up as they go).
  const [syncJob, setSyncJob] = useState<{ done: number; total: number } | null>(null);
  const syncIdsRef = useRef<number[]>([]); // rom_ids of the running one-shot batch
  const syncMissing = async () => {
    if (syncJob) return; // already running for this page
    const missing = games.filter((g) => !g.is_downloaded).map((g) => g.rom_id);
    if (missing.length === 0) { toaster.toast({ title: 'Nothing to sync', body: 'All games are already downloaded' }); return; }
    syncIdsRef.current = missing;
    setSyncJob({ done: 0, total: missing.length });
    toaster.toast({ title: 'Syncing collection', body: `Downloading ${missing.length} game${missing.length === 1 ? '' : 's'}` });
    const ok = await downloadBatch(missing, 3, (done) => setSyncJob({ done, total: missing.length }));
    setSyncJob(null);
    toaster.toast({ title: 'Sync complete', body: `${ok} of ${missing.length} downloaded` });
    refreshGames();
  };

  // Re-fetch this group's games (bypassing the stale cache) so the downloaded
  // dots reflect games pulled in by a collection sync without a plugin restart.
  const refreshGames = async () => {
    if (!group) return;
    try {
      const res = await getLibraryGames(mode, group.key);
      if (res?.success) {
        const list: LibGame[] = res.games || [];
        libCacheSet(cacheKey(group.key), list);
        setGames(list);
      }
    } catch (e) { console.error('refreshGames failed', e); }
  };

  // Background auto-sync progress for THIS collection, polled from the backend so
  // the header reflects the CollectionSyncManager's downloads — not only the
  // frontend one-shot batch. build_sync_status emits sync_state/downloaded/total.
  // pct (when present) is the backend's fine-grained byte-level percent for the
  // collection, so the bar moves continuously rather than stepping per game.
  const [autoProg, setAutoProg] = useState<{ done: number; total: number; speed: number; pct: number | null } | null>(null);
  useEffect(() => {
    if (!isCollection || isVirtual || !group) { setAutoProg(null); return; }
    let alive = true;
    const tick = async () => {
      try {
        const st = await getServiceStatus();
        const col = (st?.collections || []).find((c: any) => c.name === group.key);
        if (!alive) return;
        if (col && col.sync_state === 'syncing' && typeof col.total === 'number') {
          setAutoProg({
            done: col.downloaded || 0, total: col.total, speed: col.speed || 0,
            pct: typeof col.downloaded_pct === 'number' ? col.downloaded_pct : null,
          });
        } else {
          // Just finished a backend auto-sync pass → refresh the dots once.
          setAutoProg((prev) => { if (prev) refreshGames(); return null; });
        }
      } catch { /* transient */ }
    };
    tick();
    const id = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(id); };
  }, [isCollection, group?.key]);

  // Fine-grained fill for the one-shot batch: poll the in-flight downloads (≤
  // concurrency) and sum their percentages AND speeds, so the bar advances
  // smoothly within each game and we get a live aggregate transfer rate.
  const [syncStats, setSyncStats] = useState<{ frac: number; speed: number }>({ frac: 0, speed: 0 });
  useEffect(() => {
    if (!syncJob) { setSyncStats({ frac: 0, speed: 0 }); return; }
    let alive = true;
    const tick = async () => {
      const active = syncIdsRef.current.filter((id) => _dlActive.has(id));
      let frac = 0, speed = 0;
      await Promise.all(active.map(async (id) => {
        try {
          const p = await getDownloadProgress(id);
          if (p) {
            if (typeof p.percent === 'number') frac += p.percent / 100;
            if (typeof p.speed === 'number') speed += p.speed;
          }
        } catch { /* transient */ }
      }));
      if (alive) setSyncStats({ frac, speed });
    };
    tick();
    const id = setInterval(tick, 350);
    return () => { alive = false; clearInterval(id); };
  }, [syncJob]);

  // Unified progress: the one-shot "Sync missing" batch takes precedence (it's
  // the user's explicit action), otherwise fall back to background auto-sync.
  const prog = syncJob
    ? { done: syncJob.done, total: syncJob.total, speed: syncStats.speed }
    : autoProg;
  // Fine-grained percent (0..100): one-shot uses completed + in-flight fraction;
  // auto-sync prefers the backend's byte-level pct, falling back to the ratio.
  const progPct = !prog || !prog.total ? 0 : Math.min(100, Math.round(
    syncJob
      ? ((syncJob.done + syncStats.frac) / syncJob.total) * 100
      : (autoProg?.pct != null ? autoProg.pct : (prog.done / prog.total) * 100),
  ));
  // Remaining-time estimate from the percentage velocity (works for both paths).
  const progEta = useEtaFromPct(progPct, !!prog);

  // Remove this collection's downloaded ROMs. Per the chosen UX: turn auto-sync
  // OFF first (so the worker doesn't immediately re-download), then delete the
  // local files. Bumps reloadTick to remount the grid so download dots clear.
  const [reloadTick, setReloadTick] = useState(0);
  const doRemove = async () => {
    if (!group) return;
    const name = group.key;
    try {
      if (isSynced) {
        await toggleCollectionSync(name, false);
        setSyncOverrides((m) => ({ ...m, [name]: false }));
      }
      const ok = await deleteCollectionRoms(name);
      if (ok === false) throw new Error('backend declined');
      setGames((gs) => gs.map((g) => ({ ...g, is_downloaded: false })));
      libCacheDelete(cacheKey(name));
      setReloadTick((n) => n + 1);
    } catch (e) {
      toaster.toast({ title: 'Remove failed', body: String(e) });
    }
  };

  // Press A / activate on the games-count opens the collection action menu.
  // The destructive "Remove downloaded" arms on first select (re-opening the
  // menu as "Confirm remove") and commits on the second — matching the game-tile
  // delete affordance. Disarms after 4s.
  const removeArmedRef = useRef(false);
  const openActions = () => {
    const missing = games.filter((g) => !g.is_downloaded).length;
    const downloaded = games.filter((g) => g.is_downloaded).length;
    const armed = removeArmedRef.current;
    showContextMenu(
      <Menu label={group?.label || 'Library'} onCancel={() => { removeArmedRef.current = false; }}>
        <MenuItem disabled={!!syncJob || missing === 0} onSelected={syncMissing}>
          {`${isVirtual ? 'Download' : 'Sync'} missing${missing ? ` (${missing})` : ''}`}
        </MenuItem>
        {!isVirtual && (
          <MenuItem tone="destructive" disabled={downloaded === 0}
            onSelected={() => {
              if (!armed) {
                removeArmedRef.current = true;
                setTimeout(() => { removeArmedRef.current = false; }, 4000);
                requestAnimationFrame(openActions); // reopen showing the confirm label
              } else {
                removeArmedRef.current = false;
                doRemove();
              }
            }}>
            {armed ? 'Confirm remove' : `Remove downloaded${downloaded ? ` (${downloaded})` : ''}`}
          </MenuItem>
        )}
      </Menu>,
    );
  };

  const onButtonDown = (evt: any) => {
    const b = evt?.detail?.button;
    if (b === GamepadButton.BUMPER_LEFT) cycle(-1);
    else if (b === GamepadButton.BUMPER_RIGHT) cycle(1);
    else if (b === GamepadButton.OPTIONS) toggleSync(); // Y
    else if (b === GamepadButton.SELECT) Navigation.Navigate("/romm-sync-settings");
  };
  // Back → library index. Use onCancelButton (not a CANCEL case in onButtonDown):
  // it CONSUMES the B press so Steam's default router-back doesn't ALSO fire and
  // pop us right back into this platform.
  const onBack = () => Navigation.Navigate("/romm-sync-library");

  const jumpTo = (g: LibGroup) => {
    const from = siblings.findIndex((s) => s.key === group?.key);
    const to = siblings.findIndex((s) => s.key === g.key);
    if (from >= 0 && to >= 0) setSlideDir(to >= from ? 1 : -1);
    _libGroupHolder = { mode, group: g };
    setGroup(g);
  };

  if (!group) {
    return v2Page(<div style={{ padding: '16px', color: V2.fgMuted }}>No group selected.</div>);
  }

  const canPage = siblings.length > 1;
  const ci = siblings.findIndex((s) => s.key === group.key);
  // Sliding-track carousel: every sibling is a content-sized slot on a track
  // anchored at left:50%. We measure the selected slot's real centre offset and
  // translate the track by -that, so the selected name sits dead-centre no matter
  // its width. All slots show their full label (no truncation). Bumpers live
  // outside the clipped viewport so the edge fade never hides them.
  const DOTGAP = 16; // px gap on each side of a separator dot
  // Re-measure the selected slot's centre whenever the group/list changes.
  useLayoutEffect(() => {
    const el = selSlotRef.current;
    if (el) setSelW(el.offsetLeft + el.offsetWidth / 2);
  }, [group?.key, siblings.length]);

  // Width reserved for the right-hand status column (and mirrored by the left
  // spacer so the carousel stays screen-centred). Fixed while syncing so the
  // per-second speed/ETA text fills a constant box instead of resizing it.
  const railW = prog ? 188 : 64;

  return v2Page(
    <Focusable noFocusRing onButtonDown={onButtonDown} onCancelButton={onBack}
      onOptionsActionDescription={isCollection && !isVirtual ? (isSynced ? 'Stop syncing' : 'Sync collection') : undefined}>
      <div style={{
        position: 'sticky', top: 0, zIndex: 50,
        background: 'rgba(7,7,15,0.78)',
        backdropFilter: 'blur(20px)', WebkitBackdropFilter: 'blur(20px)',
        borderBottom: `1px solid ${V2.border}`,
        padding: '6px 0',
      }}>
        <Focusable noFocusRing ref={headerRef} style={{
          display: 'flex', alignItems: 'center', height: '46px',
        }}>
          {/* Spacer mirrors the games-count column on the right so the carousel
              stays centred on the screen, not just within the flex row. */}
          <div style={{ flexShrink: 0, width: `${railW}px`, padding: '0 4px 0 12px', boxSizing: 'border-box', transition: 'width 0.2s ease' }} />
          {canPage && <div style={{ flexShrink: 0, padding: '0 4px', zIndex: 2, display: 'flex', alignItems: 'center' }}><Bumper label="L1" /></div>}
          <div style={{
            position: 'relative', overflow: 'hidden', height: '100%', flex: 1,
            // Soft fade only at the inner edges, between bumpers and names.
            maskImage: 'linear-gradient(to right, transparent, #000 8%, #000 92%, transparent)',
            WebkitMaskImage: 'linear-gradient(to right, transparent, #000 8%, #000 92%, transparent)',
          }}>
            <div style={{
              position: 'absolute', top: 0, left: '50%', height: '100%',
              display: 'flex', alignItems: 'center',
              transform: `translateX(${-selW}px)`,
              transition: 'transform 0.32s cubic-bezier(0.22, 1, 0.36, 1)',
            }}>
              {siblings.map((g, idx) => {
                const sel = idx === ci;
                return [
                  idx > 0 && (
                    <span key={`dot-${g.key}`} style={{
                      flexShrink: 0, padding: `0 ${DOTGAP}px`,
                      color: V2.fgMuted, opacity: 0.5, fontSize: '13px',
                    }}>·</span>
                  ),
                  <Focusable noFocusRing key={g.key} ref={sel ? selSlotRef : undefined}
                    onActivate={() => jumpTo(g)} onClick={() => jumpTo(g)}
                    style={{
                      flexShrink: 0, height: '100%',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      cursor: 'pointer', boxSizing: 'border-box',
                      fontSize: sel ? '22px' : '14px',
                      fontWeight: sel ? 800 : 500,
                      letterSpacing: sel ? '-0.01em' : '0',
                      color: sel ? V2.fg : V2.fgMuted,
                      opacity: sel ? 1 : 0.5,
                      whiteSpace: 'nowrap',
                      transition: 'opacity 0.32s, color 0.32s',
                    }}>
                    {g.label}
                  </Focusable>,
                ];
              })}
            </div>
          </div>
          {canPage && <div style={{ flexShrink: 0, padding: '0 4px', zIndex: 2, display: 'flex', alignItems: 'center' }}><Bumper label="R1" /></div>}
          {/* Games count doubles as the collection action trigger: focus it and
              press A to open the actions menu (Sync missing). While a sync job is
              running it shows live progress instead of the static count. */}
          <Focusable noFocusRing onActivate={openActions} onClick={openActions}
            style={{
              flexShrink: 0, width: `${railW}px`, boxSizing: 'border-box',
              padding: '0 12px 0 4px', overflow: 'hidden',
              textAlign: 'right', fontSize: '11px', whiteSpace: 'nowrap',
              cursor: 'pointer', transition: 'width 0.2s ease',
              color: V2.fgMuted,
              display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: '5px',
            }}>
            {/* Synced indicator — same green dot as downloaded games/tiles. */}
            {isSynced && !prog && (
              <span style={{
                width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0,
                background: V2.success, boxShadow: '0 0 0 2px rgba(0,0,0,0.45)',
              }} />
            )}
            {!loading && (prog
              ? `${progPct}%${prog.speed ? `   ·   ${formatSpeed(prog.speed)}` : ''}${formatEta(progEta) ? `   ·   ${formatEta(progEta)} left` : ''}`
              : `${games.length} ${games.length === 1 ? 'game' : 'games'}`)}
          </Focusable>
        </Focusable>
        {/* Determinate sync bar — pinned to the header's bottom border, like
            Steam's achievement bar. The gradient is painted across the FULL
            width and revealed up to progPct (so the colour transition is visible
            at any fill level, not just the left stop). Fed by the unified,
            fine-grained model (one-shot batch + background auto-sync). */}
        {prog && prog.total > 0 && (
          <div style={{
            position: 'absolute', left: 0, right: 0, bottom: '-1px', height: '3px',
            overflow: 'hidden', background: 'rgba(255,255,255,0.12)',
          }}>
            {/* Gradient fill, clipped to progPct so it actually reflects progress. */}
            <div style={{
              height: '100%', width: `${progPct}%`,
              background: 'linear-gradient(90deg, #7c5cff 0%, #a18fff 45%, #5ce0ff 100%)',
              transition: 'width 0.3s ease',
            }} />
          </div>
        )}
      </div>

      {loading ? (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px' }}>Loading games…</div>
      ) : games.length === 0 ? (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px' }}>No games in this group.</div>
      ) : (
        <Focusable noFocusRing
          key={`${group.key}:${reloadTick}`}
          className={slideDir === 1 ? 'lib-slide-r' : 'lib-slide-l'}
          style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))',
            gap: '18px 16px', padding: '6px 16px',
          }}
        >
          {games.map((g, i) => (
            <GameTile key={g.rom_id} game={g} onOpen={openGame} onActiveCover={setBgUri}
              focusRef={i === 0 ? firstTileRef : undefined} />
          ))}
        </Focusable>
      )}
      <style>{`
        @keyframes libSlideR { from { transform: translateX(7%); } to { transform: translateX(0); } }
        @keyframes libSlideL { from { transform: translateX(-7%); } to { transform: translateX(0); } }
        @keyframes libFade { from { opacity: 0.55; } to { opacity: 1; } }
        .lib-slide-r { animation: libSlideR 0.34s cubic-bezier(0.22, 1, 0.36, 1) both, libFade 0.16s ease-out both; }
        .lib-slide-l { animation: libSlideL 0.34s cubic-bezier(0.22, 1, 0.36, 1) both, libFade 0.16s ease-out both; }
      `}</style>
    </Focusable>,
    bgUri,
  );
}

// Fetch an auth-gated RomM resource path as a base64 data URI (backend proxy).
function useRommImage(path: string | null): string | null {
  const [uri, setUri] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    if (!path) { setUri(null); return; }
    (async () => {
      try { const r = await qGetImage(path); if (alive) setUri(r?.data_uri || null); }
      catch { if (alive) setUri(null); }
    })();
    return () => { alive = false; };
  }, [path]);
  return uri;
}

// Fullscreen screenshot lightbox — RCarousel equivalent. L1/R1 or the
// on-screen arrows page through; A/B close.
function ScreenshotLightbox({ paths, index, closeModal }:
  { paths: string[]; index: number; closeModal?: () => void; }) {
  const [i, setI] = useState(index);
  const uri = useRommImage(paths[i]);
  const go = (d: -1 | 1) => setI((p) => (p + d + paths.length) % paths.length);
  const onButtonDown = (evt: any) => {
    const b = evt?.detail?.button;
    if (b === GamepadButton.BUMPER_LEFT) go(-1);
    else if (b === GamepadButton.BUMPER_RIGHT) go(1);
  };
  const multi = paths.length > 1;

  // Sample the left/right edge luminance of the current shot so each arrow can
  // flip to a light or dark scrim and stay legible over the image behind it.
  // true = that edge is dark → use a light scrim with a dark icon.
  const [edgeDark, setEdgeDark] = useState<{ left: boolean; right: boolean }>({ left: true, right: true });
  useEffect(() => {
    if (!uri) { setEdgeDark({ left: true, right: true }); return; }
    let alive = true;
    const img = new Image();
    img.onload = () => {
      try {
        const w = 64, h = Math.max(1, Math.round(64 * img.height / Math.max(1, img.width)));
        const c = document.createElement('canvas');
        c.width = w; c.height = h;
        const ctx = c.getContext('2d');
        if (!ctx) return;
        ctx.drawImage(img, 0, 0, w, h);
        const lum = (x0: number) => {
          const d = ctx.getImageData(x0, 0, Math.max(1, Math.round(w * 0.18)), h).data;
          let s = 0, n = 0;
          for (let k = 0; k < d.length; k += 4) { s += 0.2126 * d[k] + 0.7152 * d[k + 1] + 0.0722 * d[k + 2]; n++; }
          return n ? s / n : 0;
        };
        if (alive) setEdgeDark({ left: lum(0) < 140, right: lum(Math.round(w * 0.82)) < 140 });
      } catch { /* canvas may be tainted; keep default */ }
    };
    img.src = uri;
    return () => { alive = false; };
  }, [uri]);

  // Circular scrim arrow matching the Home CardRow chevrons, but with the
  // scrim/icon inverted on bright screenshot edges for contrast.
  const arrowStyle = (side: 'left' | 'right'): any => {
    const dark = side === 'left' ? edgeDark.left : edgeDark.right;
    return {
      position: 'absolute', [side]: '8px', zIndex: 2,
      minWidth: '36px', width: '36px', height: '36px', padding: 0, borderRadius: '50%',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: dark ? 'rgba(255,255,255,0.4)' : 'rgba(0,0,0,0.4)',
      color: dark ? '#07070f' : V2.fg2,
      border: dark ? '1px solid rgba(0,0,0,0.2)' : `1px solid ${V2.border}`,
      backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)',
      boxShadow: '0 2px 10px rgba(0,0,0,0.4)',
      transition: 'background 0.2s ease, color 0.2s ease',
    };
  };
  return (
    <ModalRoot onCancel={closeModal} onEscKeypress={closeModal}>
      <Focusable noFocusRing onButtonDown={onButtonDown}
        style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '14px' }}>
        <div style={{
          position: 'relative', width: '100%', display: 'flex',
          alignItems: 'center', justifyContent: 'center',
        }}>
          {multi && (
            <DialogButton onClick={() => go(-1)} style={arrowStyle('left')}>
              <FaChevronLeft size={15} />
            </DialogButton>
          )}
          {uri ? (
            <img src={uri} style={{
              maxWidth: '100%', maxHeight: '70vh', objectFit: 'contain',
              borderRadius: V2.radiusMd, boxShadow: V2.elev2,
            }} />
          ) : (
            <div style={{
              width: '100%', aspectRatio: '16 / 9', borderRadius: V2.radiusMd,
              background: V2.surface, display: 'flex', alignItems: 'center',
              justifyContent: 'center', color: V2.fgMuted, fontSize: '12px',
            }}>Loading…</div>
          )}
          {multi && (
            <DialogButton onClick={() => go(1)} style={arrowStyle('right')}>
              <FaChevronRight size={15} />
            </DialogButton>
          )}
        </div>
        {multi && (
          <div style={{ fontSize: '12px', color: V2.fgMuted }}>{i + 1} / {paths.length}</div>
        )}
      </Focusable>
    </ModalRoot>
  );
}

// One 16:9 screenshot thumbnail — opens the lightbox on activate.
function ScreenshotThumb({ paths, index }: { paths: string[]; index: number; }) {
  const uri = useRommImage(paths[index]);
  const [focused, setFocused] = useState(false);
  const open = () => showModal(<ScreenshotLightbox paths={paths} index={index} />);
  return (
    <Focusable noFocusRing
      onActivate={open}
      onClick={open}
      onFocus={() => setFocused(true)}
      onMouseEnter={() => setFocused(true)}
      onMouseLeave={() => setFocused(false)}
      style={{
        cursor: 'pointer', position: 'relative', aspectRatio: '16 / 9',
        borderRadius: V2.radiusChip, overflow: 'hidden', background: V2.surface,
        transform: focused ? 'scale(1.02)' : 'scale(1)',
        transition: 'transform 0.18s ease, box-shadow 0.18s ease',
        boxShadow: focused
          ? `0 8px 24px rgba(0,0,0,0.4), 0 0 0 2px ${V2.brand}`
          : 'none',
      }}>
      {uri ? (
        <img src={uri} loading="lazy" style={{
          width: '100%', height: '100%', objectFit: 'cover', display: 'block',
        }} />
      ) : null}
    </Focusable>
  );
}

// Responsive grid of screenshot thumbnails (RomM ScreenshotsTab).
function ScreenshotGrid({ paths }: { paths: string[]; }) {
  return (
    <Focusable noFocusRing style={{
      display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
      gap: '10px', padding: '6px 6px 4px',
    }}>
      {paths.map((p, i) => <ScreenshotThumb key={p || i} paths={paths} index={i} />)}
    </Focusable>
  );
}

type Achievement = {
  ra_id: number | null; title: string; description: string; points: number;
  type: string; badge_id: string | null; badge_url: string | null;
  badge_url_lock: string | null; earned: boolean;
};
type TypeFilter = 'all' | 'progression' | 'missable' | 'win_condition';
type StatusFilter = 'all' | 'earned' | 'locked';

function AchievementsTab({ achievements }: { achievements: Achievement[] }) {
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  // Drive the summary progress bar from 0 → pct after mount so it animates in.
  const [barReady, setBarReady] = useState(false);
  useEffect(() => { const t = setTimeout(() => setBarReady(true), 60); return () => clearTimeout(t); }, []);

  if (!achievements.length) {
    return (
      <div style={{ padding: '30px 0', color: V2.fgMuted, fontSize: '13px', fontStyle: 'italic', textAlign: 'center' }}>
        No achievement data for this game.
      </div>
    );
  }

  const earnedCount = achievements.filter((a) => a.earned).length;
  const totalPoints = achievements.reduce((s, a) => s + (a.points || 0), 0);
  const progressionCount = achievements.filter((a) => a.type === 'progression').length;
  const missableCount = achievements.filter((a) => a.type === 'missable').length;

  const isVisible = (a: Achievement) => {
    if (typeFilter !== 'all' && a.type !== typeFilter) return false;
    if (statusFilter === 'earned' && !a.earned) return false;
    if (statusFilter === 'locked' && a.earned) return false;
    return true;
  };

  const typeLabel = (t: string) => t === 'win_condition' ? 'Win Condition' : t.charAt(0).toUpperCase() + t.slice(1);
  const toggleStatus = (s: StatusFilter) => setStatusFilter((cur) => cur === s ? 'all' : s);

  const stat = (val: any, lbl: string, missable = false) => (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px', padding: '0 12px' }}>
      <div style={{ fontSize: '20px', fontWeight: 700, color: missable ? V2.warning : V2.fg, fontVariantNumeric: 'tabular-nums' }}>{val}</div>
      <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase', color: V2.fgMuted }}>{lbl}</div>
    </div>
  );

  const typeTagStyle = (t: string): any => {
    const base: any = { fontSize: '9.5px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', padding: '2px 7px', borderRadius: '8px' };
    if (t === 'progression') return { ...base, background: 'rgba(99,102,241,0.18)', color: V2.igdb };
    if (t === 'missable') return { ...base, background: 'rgba(251,191,36,0.18)', color: V2.warning };
    if (t === 'win_condition') return { ...base, background: 'rgba(74,222,128,0.18)', color: V2.success };
    return { ...base, background: V2.surface, color: V2.fg2 };
  };

  const filterBtn = (key: string, label: string, active: boolean, onClick: () => void, accent?: 'earned' | 'locked') => {
    let bg = V2.surface, color = V2.fg2, border = V2.border;
    if (active) {
      if (accent === 'earned') { bg = 'rgba(74,222,128,0.30)'; border = 'rgba(74,222,128,0.50)'; color = V2.fg; }
      else if (accent === 'locked') { bg = 'rgba(255,80,80,0.24)'; border = 'rgba(255,80,80,0.45)'; color = V2.fg; }
      else { bg = V2.fg; border = V2.fg; color = V2.bg; }
    }
    return (
      <Focusable noFocusRing
        key={key}
        onActivate={onClick}
        onClick={onClick}
        style={{
          background: bg, border: `1px solid ${border}`, borderRadius: V2.radiusPill,
          color, padding: '5px 13px', fontSize: '11.5px', fontWeight: 500, cursor: 'pointer',
        }}
      >
        {label}
      </Focusable>
    );
  };

  const pct = achievements.length ? Math.round((earnedCount / achievements.length) * 100) : 0;
  const EASE = 'cubic-bezier(0.22,1,0.36,1)';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      <style>{`
        @keyframes achFade { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
        .ach-fade { animation: achFade 320ms ${EASE} both; animation-delay: calc(var(--ach-i, 0) * 28ms); }
        /* Row hover is the shared .romm-row (see V2_ROW_STYLE). */
      `}</style>

      <div className="ach-fade" style={{ display: 'flex', flexDirection: 'column', background: V2.surface, border: `1px solid ${V2.border}`, borderRadius: V2.radiusLg, overflow: 'hidden' }}>
        <div style={{ display: 'flex', padding: '14px 0' }}>
          {stat(
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '8px' }}>
              {earnedCount} / {achievements.length}
              <span style={{ width: '4px', height: '4px', borderRadius: '50%', background: V2.fgMuted }} />
              {pct}%
            </span>, 'Achievements')}
          <div style={{ width: '1px', background: V2.border }} />
          {stat(totalPoints, 'Total Points')}
          {progressionCount > 0 && <><div style={{ width: '1px', background: V2.border }} />{stat(progressionCount, 'Progression')}</>}
          {missableCount > 0 && <><div style={{ width: '1px', background: V2.border }} />{stat(missableCount, 'Missable', true)}</>}
        </div>
        <div style={{ height: '4px', background: 'rgba(255,255,255,0.06)' }}>
          <div style={{
            height: '100%', width: barReady ? `${pct}%` : '0%',
            background: `linear-gradient(90deg, ${V2.brand}, ${V2.success})`,
            borderRadius: '0 2px 2px 0', transition: `width 800ms ${EASE}`,
          }} />
        </div>
      </div>

      <Focusable noFocusRing className="ach-fade" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '6px', '--ach-i': 1 } as any}>
        {(['all', 'progression', 'missable', 'win_condition'] as TypeFilter[]).map((f) =>
          filterBtn(f, typeLabel(f), typeFilter === f, () => setTypeFilter(f)))}
        <span style={{ width: '1px', height: '16px', background: V2.surfaceHover, margin: '0 4px' }} />
        {filterBtn('earned', '✓ Earned', statusFilter === 'earned', () => toggleStatus('earned'), 'earned')}
        {filterBtn('locked', '⊘ Locked', statusFilter === 'locked', () => toggleStatus('locked'), 'locked')}
      </Focusable>

      {/* Filtered-out rows simply unmount (with a fade on the survivors) so the
          list re-flows without a clipping wrapper — that lets the shared
          .romm-row outset glow show, identical to the saves list. */}
      <Focusable noFocusRing style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {achievements.filter(isVisible).map((a, i) => {
          const src = a.earned ? a.badge_url : a.badge_url_lock;
          return (
            <Focusable noFocusRing key={a.ra_id ?? i} onActivate={() => {}} className="romm-row ach-fade" style={{
              display: 'grid', gridTemplateColumns: '52px 1fr auto', gap: '14px', alignItems: 'center',
              padding: '10px 14px', borderRadius: V2.radiusMd, opacity: a.earned ? 1 : 0.55,
              '--ach-i': Math.min(i, 12) + 2,
            } as any}>
              <div style={{ width: '52px', height: '52px', borderRadius: '8px', overflow: 'hidden', background: V2.coverPlaceholder, flexShrink: 0 }}>
                {src && <img src={src} alt={a.title} loading="lazy" style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                  onError={(e) => { (e.target as HTMLImageElement).style.visibility = 'hidden'; }} />}
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: '13px', fontWeight: 600, color: V2.fg }}>{a.title}</div>
                <div style={{ fontSize: '11.5px', color: V2.fgMuted, marginTop: '2px', lineHeight: 1.4 }}>{a.description}</div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '4px', whiteSpace: 'nowrap' }}>
                <div style={{ fontSize: '12px', fontWeight: 700, color: V2.fg2, fontVariantNumeric: 'tabular-nums' }}>{a.points} pts</div>
                {a.type && <span style={typeTagStyle(a.type)}>{typeLabel(a.type)}</span>}
              </div>
            </Focusable>
          );
        })}
      </Focusable>
    </div>
  );
}

// Restore modal — a bare ModalRoot styled entirely in the RomM v2 design
// language (tokens, V2Button) rather than Steam chrome. Previews the chosen
// version (screenshot for states) and exposes Restore / Restore-as-copy /
// Cancel. closeModal is injected by showModal.
function RestoreModal({ romId, entry, shotUri, onDone, closeModal }: {
  romId: number; entry: HistoryEntry; shotUri?: string; onDone: () => void; closeModal?: () => void;
}) {
  const isState = entry.save_type === 'states';
  const [shot, setShot] = useState<string | null>(shotUri ?? null);
  // Fetch a preview for any version (saves can carry screenshots too), unless
  // we were handed a cached one. Loading runs until the fetch resolves.
  const [loadingShot, setLoadingShot] = useState(!shotUri);
  const [busy, setBusy] = useState<null | 'restore' | 'copy'>(null);
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (shotUri) return;
    getSaveScreenshot(romId, entry.id, entry.save_type)
      .then((r: any) => setShot(r?.data_uri || null))
      .catch(() => setShot(null))
      .finally(() => setLoadingShot(false));
  }, []);

  // Move controller focus into the overlay (no ModalRoot to do it for us).
  useEffect(() => {
    const t = setTimeout(() => cardRef.current?.focus(), 60);
    return () => clearTimeout(t);
  }, []);

  const run = async (asCopy: boolean) => {
    if (busy) return;
    setBusy(asCopy ? 'copy' : 'restore');
    try {
      const res = await restoreSaveVersion(romId, entry.id, entry.save_type, asCopy);
      if (res?.success) {
        toaster.toast({ title: 'Restored', body: res.tgt_name ? `→ ${res.tgt_name}` : 'Version restored' });
        onDone();
        closeModal?.();
      } else {
        toaster.toast({ title: 'Restore failed', body: res?.message || 'Unknown error' });
        setBusy(null);
      }
    } catch (e) {
      toaster.toast({ title: 'Restore failed', body: String(e) });
      setBusy(null);
    }
  };

  const meta = [slotLabel(entry), entry.device || '', fmtHistSize(entry.size_bytes)].filter(Boolean).join(' · ');

  return (
    <ModalRoot bHideCloseIcon onCancel={closeModal} onEscKeypress={closeModal}
      className="romm-modal-collapse" modalClassName="romm-modal-collapse">
    <Focusable noFocusRing
      className="romm-ui"
      onCancelButton={() => closeModal?.()}
      onButtonDown={(e: any) => { if (e?.detail?.button === GamepadButton.CANCEL) closeModal?.(); }}
      style={{
        position: 'fixed', inset: 0, zIndex: 9999,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'rgba(7,7,15,0.45)',
        WebkitBackdropFilter: 'blur(8px)', backdropFilter: 'blur(8px)',
      }}
    >
      <style>{`
        @keyframes sdShimmer { 0% { background-position: -150% 0; } 100% { background-position: 150% 0; } }
        .sd-shimmer { background-image: linear-gradient(100deg, transparent 20%, rgba(255,255,255,0.22) 50%, transparent 80%) !important; background-size: 200% 100% !important; background-repeat: no-repeat; animation: sdShimmer 1.1s linear infinite; }
        ${V2_FOCUS_STYLE}
        /* Collapse ModalRoot's own panel chrome so only our overlay shows. */
        .romm-modal-collapse, .romm-modal-collapse > div {
          background: transparent !important; border: none !important; box-shadow: none !important; padding: 0 !important;
        }
      `}</style>
      <Focusable noFocusRing autoFocus ref={cardRef} flow-children="vertical" style={{
        fontFamily: V2.font, color: V2.fg, width: '520px', maxWidth: '90vw', boxSizing: 'border-box',
        padding: '18px', display: 'flex', flexDirection: 'column', gap: '12px',
        maxHeight: '82vh', overflowY: 'auto',
        background: 'linear-gradient(180deg, rgba(20,20,30,0.7) 0%, rgba(10,10,18,0.78) 100%)',
        WebkitBackdropFilter: 'blur(28px) saturate(1.1)', backdropFilter: 'blur(28px) saturate(1.1)',
        border: `1px solid rgba(255,255,255,0.12)`, borderRadius: V2.radiusCard,
        boxShadow: '0 16px 48px rgba(0,0,0,0.55)',
      }}>
        <div>
          <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase', color: V2.brand }}>
            Restore {isState ? 'state' : 'save'}
          </div>
          <div style={{ fontSize: '20px', fontWeight: 800, marginTop: '4px' }}>{fmtHistTs(entry.updated_at)}</div>
          {meta && <div style={{ fontSize: '13px', color: V2.fg2, marginTop: '4px' }}>{meta}</div>}
        </div>

        <div className={loadingShot ? 'sd-shimmer' : ''} style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          minHeight: loadingShot || !shot ? '120px' : undefined,
          backgroundColor: (loadingShot || !shot) ? V2.coverPlaceholder : 'transparent',
          borderRadius: V2.radiusLg, overflow: 'hidden',
          border: (loadingShot || !shot) ? `1px solid ${V2.border}` : 'none',
        }}>
          {loadingShot ? (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '8px', color: V2.fgMuted }}>
              <FaSync size={16} style={{ animation: 'spin 1s linear infinite' }} />
              <span style={{ fontSize: '12px' }}>Loading preview…</span>
            </div>
          ) : shot ? (
            <img src={shot} style={{ maxWidth: '100%', maxHeight: '210px', width: 'auto', display: 'block', borderRadius: V2.radiusLg }} />
          ) : (
            <span style={{ fontSize: '12px', color: V2.fgMuted }}>No preview available</span>
          )}
        </div>

        <div style={{ fontSize: '12.5px', color: V2.fg2, lineHeight: 1.55, background: V2.surface, border: `1px solid ${V2.border}`, borderRadius: V2.radiusMd, padding: '10px 12px' }}>
          <span style={{ color: V2.fg, fontWeight: 600 }}>Restore (overwrite)</span> replaces the current file with this version. The current file is backed up first.
          {isState && <><br /><span style={{ color: V2.fg, fontWeight: 600 }}>Restore as copy</span> writes this version into a new free slot, leaving your current slots untouched.</>}
        </div>

        <Focusable noFocusRing flow-children="horizontal" style={{ display: 'flex', gap: '8px', flexWrap: 'nowrap', justifyContent: 'flex-end', alignItems: 'center' }}>
          <V2Button variant="text" disabled={!!busy} onClick={() => closeModal?.()}>Cancel</V2Button>
          {isState && (
            <V2Button variant="tonal" disabled={!!busy} onClick={() => run(true)}>
              {busy === 'copy' ? <FaSync size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <FaCopy size={12} />}
              <span>Restore as copy</span>
            </V2Button>
          )}
          <V2Button variant="danger" disabled={!!busy} onClick={() => run(false)}>
            {busy === 'restore' ? <FaSync size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <FaUndo size={12} />}
            <span>Restore (overwrite)</span>
          </V2Button>
        </Focusable>
      </Focusable>
    </Focusable>
    </ModalRoot>
  );
}

// Inline Save Data tab — Saves + States subtabs, mirrors RomM's SaveDataTab.
// Saves are a vertical info list; States are a screenshot tile grid. Selecting
// an item reveals inline restore actions (restore-in-place, and restore-as-copy
// for states) backed by the same callables the standalone history page uses.
function SaveDataTab({ romId }: { romId: number }) {
  const [sub, setSub] = useState<'saves' | 'states'>('saves');
  const [loading, setLoading] = useState(true);
  const [saves, setSaves] = useState<HistoryEntry[]>([]);
  const [states, setStates] = useState<HistoryEntry[]>([]);
  // shots[id]: 'loading' while fetching, '' once resolved with no screenshot,
  // otherwise the data URI. Absence means not yet requested.
  const [shots, setShots] = useState<Record<number, string>>({});
  const EASE = 'cubic-bezier(0.22,1,0.36,1)';

  const load = async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const res = await getSaveHistory(romId);
      if (res?.success) {
        setSaves(res.saves || []);
        setStates(res.states || []);
      } else if (!silent) {
        setSaves([]); setStates([]);
      }
    } catch (e) {
      console.error('get_save_history failed', e);
      if (!silent) { setSaves([]); setStates([]); }
    } finally {
      if (!silent) setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  // After a restore the new version is written locally and the file watcher
  // uploads it to the server a moment later. Mirror the GTK app's
  // _await_restore_sync: snapshot the current ids, wait for the upload debounce,
  // then poll once a second until a new id appears (bounded deadline).
  const pollCancelRef = useRef<(() => void) | null>(null);
  useEffect(() => () => pollCancelRef.current?.(), []);
  const reloadAfterRestore = () => {
    pollCancelRef.current?.();
    const baseline = new Set<number>([...saves, ...states].map((e) => e.id));
    let cancelled = false;
    pollCancelRef.current = () => { cancelled = true; };
    const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
    (async () => {
      await sleep(3500); // upload debounce + buffer
      const deadline = Date.now() + 20000;
      while (!cancelled) {
        try {
          const res = await getSaveHistory(romId);
          if (res?.success) {
            const ids = [...(res.saves || []), ...(res.states || [])].map((e: any) => e.id);
            const hasNew = ids.some((id) => !baseline.has(id));
            if (hasNew || Date.now() >= deadline) {
              setSaves(res.saves || []); setStates(res.states || []);
              return;
            }
          }
        } catch { /* keep polling */ }
        if (Date.now() >= deadline) return;
        await sleep(1000);
      }
    })();
  };

  // Lazily fetch state screenshots once the States subtab is opened. The
  // backend serves them one request at a time, so fetch sequentially, newest
  // first, and commit each result as it lands — tiles fill in progressively
  // instead of all appearing only once the last one finishes.
  const requestedRef = useRef<Set<number>>(new Set());
  useEffect(() => {
    if (sub !== 'states') return;
    const queue = states
      .filter((s) => !requestedRef.current.has(s.id))
      .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    if (!queue.length) return;
    queue.forEach((s) => requestedRef.current.add(s.id));
    setShots((p) => {
      const next = { ...p };
      queue.forEach((s) => { if (!(s.id in next)) next[s.id] = 'loading'; });
      return next;
    });
    let cancelled = false;
    (async () => {
      for (const s of queue) {
        if (cancelled) return;
        try {
          const r: any = await getSaveScreenshot(romId, s.id, 'states');
          setShots((p) => ({ ...p, [s.id]: r?.data_uri || '' }));
        } catch {
          setShots((p) => ({ ...p, [s.id]: '' }));
        }
      }
    })();
    return () => { cancelled = true; };
  }, [sub, states]);

  const openRestore = (e: HistoryEntry) => {
    const cached = shots[e.id];
    const shotUri = (cached && cached !== 'loading') ? cached : undefined;
    showModal(<RestoreModal romId={romId} entry={e} shotUri={shotUri} onDone={reloadAfterRestore} />);
  };

  const pill = (id: 'saves' | 'states', label: string, count: number) => {
    const active = sub === id;
    return (
      <Focusable noFocusRing
        key={id}
        onActivate={() => setSub(id)}
        onClick={() => setSub(id)}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: '7px',
          background: active ? V2.fg : V2.surface, border: `1px solid ${active ? V2.fg : V2.border}`,
          borderRadius: V2.radiusPill, color: active ? V2.bg : V2.fg2,
          padding: '5px 14px', fontSize: '12px', fontWeight: 600, cursor: 'pointer',
        }}
      >
        {label}
        <span style={{
          fontSize: '10px', fontWeight: 700, padding: '0px 6px', borderRadius: V2.radiusPill,
          background: active ? 'rgba(0,0,0,0.18)' : V2.surfaceHover, color: active ? V2.bg : V2.fgMuted,
        }}>{count}</span>
      </Focusable>
    );
  };

  // Group a type's entries by slot, newest-first within each slot (the newest
  // is the live/current file). Mirrors the standalone history browser.
  const groupBySlot = (entries: HistoryEntry[]) => {
    const groups: Record<string, HistoryEntry[]> = {};
    entries.forEach((e) => {
      const slot = (e.slot != null && e.slot !== '') ? String(e.slot) : slotLabel(e);
      (groups[slot] = groups[slot] || []).push(e);
    });
    return Object.keys(groups).sort().map((slot) => ({
      slot,
      items: groups[slot].slice().sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || ''))),
    }));
  };

  const slotHeader = (slot: string, count: number) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', margin: '2px 0' }}>
      <span style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: V2.fgMuted, whiteSpace: 'nowrap' }}>Slot: {slot}</span>
      <span style={{ flex: 1, height: '1px', background: V2.border }} />
      <span style={{ fontSize: '10px', color: V2.fgMuted, whiteSpace: 'nowrap' }}>{count} version{count === 1 ? '' : 's'}</span>
    </div>
  );

  const currentChip = (
    <span style={{ fontSize: '9.5px', fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', padding: '1px 7px', borderRadius: V2.radiusPill, background: 'rgba(74,222,128,0.18)', color: V2.success }}>Current</span>
  );

  if (loading) {
    return <div style={{ color: V2.fgMuted, fontSize: '12px', padding: '12px 0' }}>Loading save data…</div>;
  }

  const total = saves.length + states.length;
  if (total === 0) {
    return <div style={{ color: V2.fgMuted, fontSize: '13px', padding: '24px 0', textAlign: 'center', fontStyle: 'italic' }}>
      No server saves or states for this game yet.
    </div>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      <style>{`
        @keyframes sdFade { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .sd-fade { animation: sdFade 300ms ${EASE} both; animation-delay: calc(var(--sd-i, 0) * 26ms); }
        /* Row/tile hover is the shared .romm-row / .romm-tile (see V2_ROW_STYLE). */
        @keyframes sdShimmer { 0% { background-position: -150% 0; } 100% { background-position: 150% 0; } }
        .sd-shimmer { background-image: linear-gradient(100deg, transparent 20%, rgba(255,255,255,0.22) 50%, transparent 80%) !important; background-size: 200% 100% !important; background-repeat: no-repeat; animation: sdShimmer 1.1s linear infinite; }
      `}</style>

      <Focusable noFocusRing flow-children="horizontal" style={{ display: 'flex', gap: '8px' }}>
        {pill('saves', 'Saves', saves.length)}
        {pill('states', 'States', states.length)}
      </Focusable>

      {sub === 'saves' ? (
        saves.length === 0 ? (
          <div style={{ color: V2.fgMuted, fontSize: '12px', padding: '12px 0' }}>No saves for this game.</div>
        ) : (
          <Focusable noFocusRing style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {groupBySlot(saves).map((g) => (
              <div key={`sg-${g.slot}`} style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {slotHeader(g.slot, g.items.length)}
                {g.items.map((e, idx) => {
                  const isCur = idx === 0;
                  const sub2 = [e.device || '', fmtHistSize(e.size_bytes)].filter(Boolean).join(' · ');
                  return (
                    <div key={`save-${e.id}`} className="sd-fade" style={{ '--sd-i': Math.min(idx, 14) } as any}>
                      <Focusable noFocusRing
                        className="romm-row"
                        onActivate={() => openRestore(e)}
                        onClick={() => openRestore(e)}
                        style={{
                          display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', textAlign: 'left',
                          padding: '10px 13px', cursor: 'pointer', borderRadius: V2.radiusMd,
                        }}
                      >
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '3px', minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <span style={{ fontSize: '13px', fontWeight: 600, color: V2.fg }}>{fmtHistTs(e.updated_at)}</span>
                            {isCur && currentChip}
                          </div>
                          {sub2 && <div style={{ fontSize: '11px', color: V2.fgMuted }}>{sub2}</div>}
                        </div>
                        <FaUndo size={13} style={{ color: V2.fgMuted, flexShrink: 0 }} />
                      </Focusable>
                    </div>
                  );
                })}
              </div>
            ))}
          </Focusable>
        )
      ) : (
        states.length === 0 ? (
          <div style={{ color: V2.fgMuted, fontSize: '12px', padding: '12px 0' }}>No states for this game.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {(() => {
              const pending = states.filter((s) => shots[s.id] === undefined || shots[s.id] === 'loading').length;
              if (pending === 0) return null;
              const loaded = states.length - pending;
              return (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '11px', color: V2.fgMuted }}>
                  <FaSync size={11} style={{ animation: 'spin 1s linear infinite' }} />
                  <span>Loading previews {loaded}/{states.length}…</span>
                </div>
              );
            })()}
            {groupBySlot(states).map((g) => (
              <div key={`stg-${g.slot}`} style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {slotHeader(g.slot, g.items.length)}
                <Focusable noFocusRing style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '12px' }}>
                  {g.items.map((e, idx) => {
                    const isCur = idx === 0;
                    const shot = shots[e.id];
                    const loadingShot = shot === undefined || shot === 'loading';
                    return (
                      <Focusable noFocusRing
                        key={`state-${e.id}`}
                        className="romm-tile sd-fade"
                        onActivate={() => openRestore(e)}
                        onClick={() => openRestore(e)}
                        style={{
                          display: 'flex', flexDirection: 'column', cursor: 'pointer', overflow: 'hidden',
                          borderRadius: V2.radiusLg,
                          '--sd-i': Math.min(idx, 14),
                        } as any}
                      >
                        <div className={loadingShot ? 'sd-shimmer' : ''} style={{ position: 'relative', aspectRatio: '16 / 9', backgroundColor: V2.coverPlaceholder, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
                          {loadingShot ? (
                            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px', color: V2.fgMuted }}>
                              <FaSync size={14} style={{ animation: 'spin 1s linear infinite' }} />
                              <span style={{ fontSize: '10px' }}>Downloading…</span>
                            </div>
                          ) : shot ? (
                            <img src={shot} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
                          ) : (
                            <span style={{ fontSize: '10px', color: V2.fgMuted }}>No screenshot</span>
                          )}
                          {isCur && <div style={{ position: 'absolute', top: '6px', left: '6px' }}>{currentChip}</div>}
                        </div>
                        <div style={{ padding: '8px 10px' }}>
                          <div style={{ fontSize: '11px', color: V2.fg2 }}>{fmtHistTs(e.updated_at)}</div>
                        </div>
                      </Focusable>
                    );
                  })}
                </Focusable>
              </div>
            ))}
          </div>
        )
      )}
    </div>
  );
}

// ── Multi-disc helpers (shared by the cover tile, the Play button and the
// Files tab) ────────────────────────────────────────────────────────────────
type LocalDisc = { name: string; path: string; is_m3u: boolean; is_region?: boolean };

// Friendly label for a disc file: keep the "(Disc N)" tail when present, else
// fall back to the bare filename (sans extension).
function discDisplayLabel(fname: string): string {
  const base = fname.replace(/\.[^.]+$/, '');
  const m = base.match(/\(dis[ck]\s*\d+[^)]*\)/i);
  return m ? m[0].replace(/[()]/g, '') : base;
}

// Friendly label for a regional variant file: surface the "(Region)" tag
// (e.g. "(Italy)", "(USA, Europe)") that RomM/No-Intro dumps carry, else the
// bare filename (sans extension).
function regionDisplayLabel(fname: string): string {
  const base = fname.replace(/\.[^.]+$/, '');
  const m = base.match(/\(([^)]+)\)\s*$/);
  return m ? m[1] : base;
}

// Launch a game. Under gamescope (Steam Deck Gaming Mode) this routes through
// the RomM tile's session-host so the Steam overlay works: prepare_steam_launch
// writes the emulator argv, then we RunGame the tile and the host execs it as a
// Steam-tracked child. Anywhere that fails (not gamescope, no tile, RunGame
// unavailable) it falls back to the direct daemon launch.
async function launchGameSmart(romId: number, disc: string | null = null,
  siblingRomId: number | null = null): Promise<any> {
  try {
    // Re-resolve the live tile appid: SetShortcutExe renumbers it (appid is a
    // hash of exe+name), so a cached _rommAppId can be stale.
    const liveIds = _rommAppIds();
    const appId = (liveIds.length ? liveIds[0] : (_rommAppId ?? await findRommShortcut()));
    if (appId != null) {
      _rommAppId = appId;
      const prep = await prepareSteamLaunch(romId, disc, siblingRomId);
      if (prep?.steam_host) {
        _rommLaunchPending = true;
        try {
          // A non-Steam shortcut is launched by its 64-bit gameID, not the bare
          // 32-bit appid: gameID = (appid << 32) | 0x02000000 (shortcut tag).
          // This is the same value GameActionStart reports back to the intercept.
          const gid = ((BigInt(appId) << 32n) | 0x2000000n).toString();
          await _sc()?.Apps?.RunGame?.(gid, "", -1, 100);
          return { success: true, message: 'Launching' };
        } catch (e) {
          _rommLaunchPending = false;
          console.error('[RomM] RunGame', e);
        }
      } else if (prep && prep.success === false && prep.steam_host === false
                 && prep.message && prep.message !== 'Not running under gamescope') {
        // A real failure (e.g. game not downloaded) — surface it rather than
        // silently falling back to a direct launch that would fail the same way.
        return prep;
      }
    }
  } catch (e) { console.error('[RomM] launchGameSmart', e); }
  return await launchGame(romId, disc, siblingRomId);
}

async function runLaunch(romId: number, gameName: string, disc: string | null,
  label?: string, setBusy?: (b: any) => void, onDone?: () => void, siblingRomId?: number | null) {
  if (setBusy) setBusy('launch');
  try {
    const r = await launchGameSmart(romId, disc, siblingRomId ?? null);
    if (r?.success) toaster.toast({ title: 'Launching', body: label || gameName });
    else toaster.toast({ title: 'Launch failed', body: r?.message || 'Error' });
  } catch (e) {
    toaster.toast({ title: 'Launch failed', body: String(e) });
  } finally {
    if (setBusy) setBusy(null);
    if (onDone) onDone();
  }
}

// A check glyph marking the disc a plain Play will currently boot.
function discMark(active: boolean) {
  return active
    ? <FaCheck size={11} style={{ marginRight: '8px', color: V2.brand }} />
    : <span style={{ display: 'inline-block', width: '11px', marginRight: '8px' }} />;
}

// Native context menu listing the bootable discs. The first item launches the
// .m3u playlist (in-game disc swap) when present, else disc 1. `last` is the
// remembered disc (what a plain Play resumes) and is checkmarked. Choosing the
// playlist persists the .m3u name so a later plain Play resumes the playlist.
function openDiscPicker(romId: number, gameName: string, discs: LocalDisc[],
  last?: string, setBusy?: (b: any) => void, onLaunched?: () => void) {
  // A regional multi-file ROM has no playlist — every entry is a standalone
  // region. Present it as a region picker (no "all discs" default).
  const isRegion = discs.length > 0 && discs.every((d) => d.is_region);
  if (isRegion) {
    // Default to the remembered region, else the first one.
    const activeName = (last && discs.some((d) => d.name === last)) ? last : discs[0]?.name;
    showContextMenu(
      <Menu label="Select region">
        {discs.map((d) => (
          <MenuItem key={d.name} onSelected={() =>
            runLaunch(romId, gameName, d.name, regionDisplayLabel(d.name), setBusy, onLaunched)}>
            {discMark(d.name === activeName)}{regionDisplayLabel(d.name)}
          </MenuItem>
        ))}
      </Menu>
    );
    return;
  }
  const m3u = discs.find((d) => d.is_m3u);
  const pickable = discs.filter((d) => !d.is_m3u);
  // The playlist is the active default when it is the remembered choice, or when
  // nothing is remembered yet (the implicit first-launch default).
  const m3uActive = !!m3u && (last === m3u.name || !last);
  showContextMenu(
    <Menu label="Select disc">
      <MenuItem onSelected={() => runLaunch(romId, gameName, m3u ? m3u.name : null,
        m3u ? 'All discs' : undefined, setBusy, onLaunched)}>
        {discMark(m3uActive)}{m3u ? 'Play (all discs, in-game swap)' : 'Play (disc 1)'}
      </MenuItem>
      {pickable.map((d) => (
        <MenuItem key={d.name} onSelected={() =>
          runLaunch(romId, gameName, d.name, discDisplayLabel(d.name), setBusy, onLaunched)}>
          {discMark(last === d.name)}{discDisplayLabel(d.name)}
        </MenuItem>
      ))}
    </Menu>
  );
}

function openRegionPicker(
  romId: number, gameName: string,
  siblings: { rom_id: number; name: string }[],
  downloadedIds: Set<number>,
  lastUsedId?: number,
  onSelected?: (sibRomId: number) => void,
) {
  const mainEntry = { rom_id: romId, name: gameName };
  const sorted = [...siblings].sort((a, b) => {
    const aDl = downloadedIds.has(a.rom_id) ? 0 : 1;
    const bDl = downloadedIds.has(b.rom_id) ? 0 : 1;
    if (aDl !== bDl) return aDl - bDl;
    return a.name.localeCompare(b.name);
  });
  const all = [mainEntry, ...sorted];

  showContextMenu(
    <Menu label="Select region">
      {all.map((entry) => (
        <MenuItem key={entry.rom_id} onSelected={() => onSelected?.(entry.rom_id)}>
          {entry.rom_id === lastUsedId ? '✓ ' : ''}
          {entry.name}
          {entry.rom_id !== romId && !downloadedIds.has(entry.rom_id) ? ' (not downloaded)' : ''}
          {downloadedIds.has(entry.rom_id) ? ' ✓' : ''}
        </MenuItem>
      ))}
    </Menu>
  );
}

function GameDetailPage() {
  const game = _libGameHolder;
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<null | 'download' | 'delete' | 'launch'>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [isDownloaded, setIsDownloaded] = useState<boolean>(!!game?.is_downloaded);
  const [discs, setDiscs] = useState<LocalDisc[]>([]);
  const [discLast, setDiscLast] = useState<string>('');
  const [bgUri, setBgUri] = useState<string | null>(null);
  const [tab, setTab] = useState('overview');
  const dlProg = useDownloadProgress(game?.rom_id ?? -1, busy === 'download');
  const dlPct = dlProg?.percent ?? null;
  const globalDownloading = useIsDownloading(game?.rom_id ?? -1);
  const downloading = busy === 'download' || globalDownloading;
  const smoothPct = useSmoothNumber(dlPct, downloading);

  const load = async () => {
    if (!game) { setLoading(false); return; }
    setLoading(true);
    try {
      const res = await getGameDetail(game.rom_id);
      if (res?.success) {
        setDetail(res);
        setIsDownloaded(!!res.is_downloaded);
      }
    } catch (e) {
      console.error('get_game_detail failed', e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  // If a download for this game finishes on another surface (e.g. its cover
  // tile), refresh so the CTA flips Download → Play here too.
  const prevDownloading = useRef(false);
  useEffect(() => {
    if (prevDownloading.current && !downloading) load();
    prevDownloading.current = downloading;
  }, [downloading]);

  // Resolve the on-disk disc files once the game is downloaded. A multi-disc
  // game exposes >1 entry (plus an .m3u); a single-disc game returns [].
  // `discReload` bumps after a disc launch so the remembered-disc checkmark and
  // the resume target stay current without leaving the page.
  const [discReload, setDiscReload] = useState(0);
  useEffect(() => {
    if (!game || !isDownloaded) { setDiscs([]); setDiscLast(''); return; }
    let alive = true;
    (async () => {
      try {
        const r = await getLocalDiscs(game.rom_id);
        if (alive) { setDiscs(r?.success ? (r.discs || []) : []); setDiscLast(r?.last || ''); }
      } catch { if (alive) { setDiscs([]); setDiscLast(''); } }
    })();
    return () => { alive = false; };
  }, [isDownloaded, game?.rom_id, discReload]);

  // A game is "multi-disc" for picker purposes when more than one bootable
  // disc exists (the .m3u playlist alone does not count as a choice).
  const pickableDiscs = discs.filter((d) => !d.is_m3u);
  const isMultiDisc = pickableDiscs.length > 1;

  const doDownload = async () => {
    if (!game) return;
    setBusy('download');
    _setDlActive(game.rom_id, true);
    try {
      const start = await downloadGame(game.rom_id);
      if (!start?.success) {
        toaster.toast({ title: 'Download failed', body: start?.message || 'Unknown error' });
        return;
      }
      const res = await awaitDownload(game.rom_id);
      if (res.ok) {
        toaster.toast({ title: 'Downloaded', body: detail?.name || game.name });
        setIsDownloaded(true);
      } else {
        toaster.toast({ title: 'Download failed', body: res.message || 'Unknown error' });
      }
    } catch (e) {
      toaster.toast({ title: 'Download failed', body: String(e) });
    } finally {
      _setDlActive(game.rom_id, false);
      setBusy(null);
    }
  };

  const doLaunch = async (disc?: string | null, label?: string) => {
    if (!game) return;
    await runLaunch(game.rom_id, detail?.name || game.name, disc ?? null, label, setBusy);
    if (disc) setDiscReload((n) => n + 1);  // refresh remembered-disc marker
  };

  // Y / Options on the Play button: pick which disc to boot.
  const openDiscMenu = () => {
    if (game) openDiscPicker(game.rom_id, detail?.name || game.name, discs, discLast,
      setBusy, () => setDiscReload((n) => n + 1));
  };

  const doDelete = async () => {
    if (!game) return;
    setBusy('delete');
    try {
      const res = await deleteGame(game.rom_id);
      if (res?.success) {
        setIsDownloaded(false);
      } else {
        toaster.toast({ title: 'Delete failed', body: res?.message || 'Unknown error' });
      }
    } catch (e) {
      toaster.toast({ title: 'Delete failed', body: String(e) });
    } finally {
      setBusy(null);
      setConfirmDelete(false);
    }
  };

  if (!game) {
    return v2Page(<div style={{ padding: '16px', color: V2.fgMuted }}>No game selected.</div>);
  }

  const name = detail?.name || game.name;
  const platform = detail?.platform || game.platform;
  const releaseDate = fmtReleaseDate(detail?.release_date);
  // RomM GameHeader meta row: platform-icon + platform · release date ·
  // verified — text items only (the tag chips render after as RTags).
  const meta: { text: string; color?: string }[] = [];
  if (platform) meta.push({ text: platform });
  if (releaseDate) meta.push({ text: releaseDate });

  // Header tag chips (RomM GameHeader): regions (info), languages (brand),
  // custom tags (neutral) — each an RTag.
  const headerTags: { text: string; tone: 'info' | 'brand' | 'neutral' }[] = [
    ...((detail?.regions || []) as string[]).map((r) => ({ text: r, tone: 'info' as const })),
    ...((detail?.languages || []) as string[]).map((l) => ({ text: l, tone: 'brand' as const })),
    ...((detail?.tags || []) as string[]).map((t) => ({ text: t, tone: 'neutral' as const })),
  ];

  // Overview "InfoGrid" sections — icon + label + chip items (RomM InfoGrid).
  const infoGrid: { label: string; items: string[]; icon?: any }[] = [];
  if (detail?.genres?.length) infoGrid.push({ label: 'Genres', items: detail.genres });
  if (detail?.companies?.length) infoGrid.push({ label: 'Companies', items: detail.companies });
  if (detail?.franchises?.length) infoGrid.push({ label: 'Franchises', items: detail.franchises });
  if (detail?.collections?.length) infoGrid.push({ label: 'Collections', items: detail.collections });

  const related = detail?.related || {};
  const hasRelated = ['expansions', 'dlcs', 'remakes', 'remasters']
    .some((k) => (related[k] || []).length);
  const ageRatings = detail?.age_ratings || [];
  const userCollections: string[] = detail?.user_collections || [];

  // Tab strip — Files only when the server reported files (RomM hides empty tabs).
  const tabList: { id: string; label: string }[] = [{ id: 'overview', label: 'Overview' }];
  if (detail?.files?.length) tabList.push({ id: 'files', label: 'Files' });
  if (detail?.screenshots?.length) tabList.push({ id: 'screenshots', label: 'Screenshots' });
  tabList.push({ id: 'save-data', label: 'Save Data' });
  if (detail?.achievements?.length) tabList.push({ id: 'achievements', label: 'Achievements' });
  tabList.push({ id: 'metadata', label: 'Metadata' });

  // L1 / R1 page through the detail tabs (RomM pages detail tabs with bumpers).
  const cycleTab = (dir: -1 | 1) => {
    const i = tabList.findIndex((t) => t.id === tab);
    const ni = (i < 0 ? 0 : i + dir + tabList.length) % tabList.length;
    setTab(tabList[ni].id);
  };
  const onButtonDown = (evt: any) => {
    const b = evt?.detail?.button;
    if (b === GamepadButton.BUMPER_LEFT) cycleTab(-1);
    else if (b === GamepadButton.BUMPER_RIGHT) cycleTab(1);
    else if (b === GamepadButton.SELECT) Navigation.Navigate("/romm-sync-settings");
  };
  // Back returns to the page this game was opened from (collection/platform games
  // page or the library index). onCancelButton CONSUMES B so Steam's default
  // router-back doesn't also fire (which would land somewhere else entirely).
  const onBack = () => Navigation.Navigate(_libGameOrigin);

  return v2Page(
    <Focusable noFocusRing onButtonDown={onButtonDown} onCancelButton={onBack} style={{ padding: '20px 16px' }}>
      <Focusable noFocusRing flow-children="horizontal" style={{ display: 'flex', gap: '22px', alignItems: 'flex-start' }}>
        {/* Cover */}
        <div style={{ flex: '0 0 220px', maxWidth: '220px' }}>
          <div style={{ boxShadow: V2.elev2, borderRadius: V2.radiusLg, overflow: 'hidden' }}>
            <GameCover romId={game.rom_id} hasCover={game.has_cover || !!detail?.has_cover} large
              radius={V2.radiusLg} onLoaded={setBgUri} />
          </div>
        </div>

        {/* Info + actions */}
        <Focusable noFocusRing flow-children="vertical" style={{ flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column', gap: '14px' }}>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 800, lineHeight: '1.15', letterSpacing: '-0.01em' }}>{name}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0', marginTop: '8px', fontSize: '13.5px', color: V2.fg2 }}>
              {/* Platform icon leads the meta row (RomM GameHeader). */}
              {game.platform_slug && (
                <span style={{ display: 'inline-flex', width: '16px', height: '16px', marginRight: '6px', alignItems: 'center', justifyContent: 'center' }}>
                  <PlatformIcon slug={game.platform_slug} size={16} />
                </span>
              )}
              {meta.map((m, i) => (
                <span key={i} style={{ display: 'inline-flex', alignItems: 'center' }}>
                  {i > 0 && <span style={{ opacity: 0.3, margin: '0 8px' }}>·</span>}
                  <span style={{ color: m.color || V2.fg2 }}>{m.text}</span>
                </span>
              ))}
              {/* Verified — icon-only check matching RomM GameHeader
                  (mdi-check-decagram seal); MdVerified is its react-icons twin. */}
              {detail?.verified && (
                <>
                  <span style={{ opacity: 0.3, margin: '0 8px' }}>·</span>
                  <MdVerified size={17} color={V2.success} />
                </>
              )}
              {/* Region / language / custom tag chips (RomM RTags). */}
              {headerTags.length > 0 && (
                <>
                  <span style={{ opacity: 0.3, margin: '0 8px' }}>·</span>
                  <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: '6px' }}>
                    {headerTags.map((tg, i) => {
                      const palette =
                        tg.tone === 'info' ? { c: '#93c5fd', b: 'rgba(147,197,253,0.14)', br: 'rgba(147,197,253,0.30)' }
                        : tg.tone === 'brand' ? { c: V2.brandHover, b: 'rgba(139,116,232,0.16)', br: 'rgba(139,116,232,0.30)' }
                        : { c: V2.fg2, b: V2.surface, br: V2.border };
                      return (
                        <span key={i} style={{
                          fontSize: '11px', fontWeight: 600, lineHeight: 1.6, padding: '1px 8px',
                          borderRadius: V2.radiusPill, color: palette.c,
                          background: palette.b, border: `1px solid ${palette.br}`,
                        }}>{tg.text}</span>
                      );
                    })}
                  </span>
                </>
              )}
            </div>
          </div>

          {/* Actions — RomM GameActions ribbon: an emphasized white pill for the
              primary CTA (Download when absent, Play when present) + circular
              surface icon buttons for the secondary actions (Delete). */}
          <Focusable noFocusRing flow-children="horizontal" style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', alignItems: 'center' }}>
            {!isDownloaded ? (
              <GameActionButton variant="emphasized" disabled={!!busy || downloading} onClick={doDownload}
                progress={downloading ? (dlPct ?? 0) : undefined}
                label={downloading
                  ? (dlPct != null ? `Downloading… ${smoothPct}%` : 'Downloading…')
                  : 'Download'}
                icon={downloading
                  ? <FaSync size={15} style={{ animation: 'spin 1s linear infinite' }} />
                  : <FaDownload size={15} />} />
            ) : !confirmDelete ? (
              <>
                <GameActionButton variant="emphasized" disabled={!!busy}
                  onClick={() => doLaunch()}
                  onOptionsButton={isMultiDisc ? openDiscMenu : undefined}
                  optionsHint={isMultiDisc}
                  label={busy === 'launch' ? 'Launching…' : 'Play'}
                  icon={busy === 'launch'
                    ? <FaSync size={15} style={{ animation: 'spin 1s linear infinite' }} />
                    : <FaPlay size={14} style={{ marginLeft: '2px' }} />} />
                <GameActionButton variant="surface" accent="danger" onClick={() => setConfirmDelete(true)}
                  icon={<FaTrash size={15} />} />
              </>
            ) : (
              <>
                <GameActionButton variant="danger" disabled={!!busy} onClick={doDelete}
                  label={busy === 'delete' ? 'Deleting…' : 'Confirm delete'}
                  icon={busy === 'delete'
                    ? <FaSync size={15} style={{ animation: 'spin 1s linear infinite' }} />
                    : <FaTrash size={15} />} />
                <GameActionButton variant="surface" onClick={() => setConfirmDelete(false)}
                  icon={<FaTimes size={16} />} />
              </>
            )}
            {/* Live transfer readout — speed · ETA, shown beside the button only
                while a download is in flight (kept off the cover tiles by design). */}
            {downloading && dlProg && (dlProg.speed > 0 || dlProg.eta > 0) && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: '8px', fontSize: '13px', color: V2.fg2,
                animation: 'dlValIn 0.35s ease' }}>
                <span style={{ opacity: 0.3 }}>·</span>
                {dlProg.speed > 0 && <span>{formatSpeed(dlProg.speed)}</span>}
                {dlProg.speed > 0 && formatEta(dlProg.eta) && <span style={{ opacity: 0.3 }}>·</span>}
                {formatEta(dlProg.eta) && <span>{formatEta(dlProg.eta)} left</span>}
              </span>
            )}
          </Focusable>

          {/* Tabbed panel (RomM GameDetails: RTabNav + tab content). L1/R1 page tabs. */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <div style={{ flex: '1 1 auto', minWidth: 0 }}><V2TabNav tabs={tabList} active={tab} onTab={setTab} /></div>
            <Bumper label="L1" />
            <Bumper label="R1" />
          </div>
          <div style={{ paddingTop: '14px' }}>
            {loading ? (
              <div style={{ color: V2.fgMuted, fontSize: '12px' }}>Loading details…</div>
            ) : tab === 'overview' ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '30px' }}>
                {/* 1. Summary */}
                {detail?.summary && (
                  <div style={{ fontSize: '13.5px', color: V2.fg2, lineHeight: '1.7' }}>{detail.summary}</div>
                )}

                {/* 2. Left-labelled fact rows — Last played · Players · Age rating
                    (RomM OverviewTab __facts). */}
                {(() => {
                  const lp = detail?.last_played ? new Date(detail.last_played) : null;
                  const lpStr = lp && !isNaN(lp.getTime()) ? lp.toLocaleString() : null;
                  const rows: { label: string; field: any }[] = [];
                  if (lpStr) rows.push({ label: 'Last played', field: <span style={{ fontSize: '13px', color: V2.fg2 }}>{lpStr}</span> });
                  if (detail?.player_count) rows.push({ label: 'Players', field: <PlayerCountBadge value={String(detail.player_count)} /> });
                  if (ageRatings.length) rows.push({ label: 'Age rating', field: <AgeRatingBadges items={ageRatings} /> });
                  if (userCollections.length) rows.push({
                    label: 'Collections',
                    field: (
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                        {userCollections.map((c, i) => (
                          <span key={i} style={{
                            display: 'inline-flex', alignItems: 'center', gap: '5px', padding: '4px 10px',
                            background: V2.surface, border: `1px solid ${V2.borderStrong}`, borderRadius: V2.radiusPill,
                            fontSize: '11.5px', fontWeight: 600, color: V2.fg2,
                          }}><FaBookmark size={10} color={V2.brand} />{c}</span>
                        ))}
                      </div>
                    ),
                  });
                  if (!rows.length) return null;
                  return (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                      {rows.map((r) => (
                        <div key={r.label} style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                          <div style={{ width: '120px', flexShrink: 0, fontSize: '10.5px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: V2.fgFaint }}>{r.label}</div>
                          <div style={{ flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: '12px' }}>{r.field}</div>
                        </div>
                      ))}
                    </div>
                  );
                })()}

                {/* 3. Info grid */}
                <InfoGrid sections={infoGrid} />

                {/* 4. HLTB */}
                {detail?.hltb && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    <SectionHeading icon={<FaClock size={12} />}>How long to beat</SectionHeading>
                    <HLTBStrip hltb={detail.hltb} />
                  </div>
                )}

                {/* 5. Related games — one labelled section per category. */}
                {hasRelated && (
                  <>
                    <RelatedSection icon={<FaPuzzlePiece size={12} />} title="Expansions" items={related.expansions} />
                    <RelatedSection icon={<FaBoxOpen size={12} />} title="DLC" items={related.dlcs} />
                    <RelatedSection icon={<FaRedo size={12} />} title="Remakes" items={related.remakes} />
                    <RelatedSection icon={<FaClone size={12} />} title="Remasters" items={related.remasters} />
                  </>
                )}

                {!detail?.summary && infoGrid.length === 0 && !hasRelated && !detail?.hltb && !ageRatings.length && (
                  <div style={{ color: V2.fgMuted, fontSize: '12px' }}>No metadata available.</div>
                )}
              </div>
            ) : tab === 'files' ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
                {/* Discs — bootable entries on disk (downloaded multi-disc games).
                    Each row launches that disc; the playlist row boots all discs
                    with in-game swapping. */}
                {isMultiDisc && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    <SectionHeading icon={<FaLayerGroup size={12} />}>Discs</SectionHeading>
                    {discs.some((d) => d.is_m3u) && (
                      <V2SettingsRow icon={<FaLayerGroup size={14} />}
                        title="All discs" subtitle="In-game disc swapping"
                        onClick={() => doLaunch(null, 'All discs')}
                        right={<FaPlay size={12} style={{ color: V2.fgMuted }} />} />
                    )}
                    {pickableDiscs.map((d) => (
                      <V2SettingsRow key={d.name} icon={<FaClone size={14} />}
                        title={discDisplayLabel(d.name)} subtitle={d.name}
                        onClick={() => doLaunch(d.name, discDisplayLabel(d.name))}
                        right={<FaPlay size={12} style={{ color: V2.fgMuted }} />} />
                    ))}
                  </div>
                )}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {isMultiDisc && <SectionHeading icon={<FaBoxOpen size={12} />}>Files</SectionHeading>}
                  {(detail?.files || []).length === 0 ? (
                    <div style={{ color: V2.fgMuted, fontSize: '12px' }}>No file information.</div>
                  ) : (detail.files).map((f: any, i: number) => (
                    <div key={i} style={{
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px',
                      padding: '9px 12px', borderRadius: V2.radiusMd, background: V2.surface, fontSize: '12px',
                    }}>
                      <span style={{ color: V2.fg2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name}</span>
                      <span style={{ color: V2.fgMuted, flexShrink: 0 }}>{fmtBytes(f.size)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : tab === 'screenshots' ? (
              <ScreenshotGrid paths={detail?.screenshots || []} />
            ) : tab === 'save-data' ? (
              <SaveDataTab romId={game.rom_id} />
            ) : tab === 'metadata' ? (
              <MetadataTab detail={detail} />
            ) : (
              <AchievementsTab achievements={detail?.achievements || []} />
            )}
          </div>
        </Focusable>
      </Focusable>
    </Focusable>,
    bgUri,
  );
}

// ── V2 settings primitives (full-screen, controller-first) ──────────────────
// Uppercase eyebrow + a column of rows, matching the GameDetails section look.
function V2SettingsSection({ title, children }: { title: string; children: any }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '24px' }}>
      <div style={{
        fontSize: '11px', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase',
        color: V2.fgMuted, padding: '0 2px',
      }}>{title}</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>{children}</div>
    </div>
  );
}

// A focusable surface row: leading icon, title + optional subtitle, optional
// trailing control. Acts as a button when onClick is given.
function V2SettingsRow({ icon, title, subtitle, onClick, right, danger, disabled }:
  { icon?: any; title: any; subtitle?: any; onClick?: () => void; right?: any; danger?: boolean; disabled?: boolean }) {
  const [active, setActive] = useState(false);
  const interactive = !!onClick && !disabled;
  const accent = danger ? V2.danger : V2.brand;
  return (
    <Focusable noFocusRing
      onActivate={interactive ? onClick : undefined}
      onClick={interactive ? onClick : undefined}
      onFocus={() => setActive(true)} onBlur={() => setActive(false)}
      onMouseEnter={() => setActive(true)} onMouseLeave={() => setActive(false)}
      style={{
        display: 'flex', alignItems: 'center', gap: '14px', padding: '14px 16px',
        borderRadius: V2.radiusCard, background: active ? V2.surfaceHover : V2.surface,
        border: `1px solid ${active && interactive ? accent : V2.border}`,
        boxShadow: active && interactive ? `0 0 0 2px ${accent}` : 'none',
        cursor: interactive ? 'pointer' : 'default', opacity: disabled ? 0.5 : 1,
        transition: 'background 0.15s, border-color 0.15s, box-shadow 0.15s',
      }}>
      {icon && (
        <div style={{
          flexShrink: 0, width: '36px', height: '36px', borderRadius: V2.radiusMd,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: danger ? 'rgba(255,80,80,0.12)' : V2.bgElevated,
          color: danger ? V2.danger : V2.brandHover,
        }}>{icon}</div>
      )}
      <div style={{ flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column', gap: '2px' }}>
        <div style={{ fontSize: '14px', fontWeight: 600, color: danger ? V2.danger : V2.fg }}>{title}</div>
        {subtitle && <div style={{ fontSize: '12px', color: V2.fgMuted, lineHeight: 1.35 }}>{subtitle}</div>}
      </div>
      {right != null && <div style={{ flexShrink: 0 }}>{right}</div>}
    </Focusable>
  );
}

// Small pill switch in the V2 palette (the row owns activation).
function V2Switch({ checked }: { checked: boolean }) {
  return (
    <div style={{
      width: '40px', height: '22px', borderRadius: V2.radiusPill, flexShrink: 0,
      background: checked ? V2.brand : 'rgba(255,255,255,0.18)',
      transition: 'background 0.15s', position: 'relative',
    }}>
      <div style={{
        position: 'absolute', top: '2px', left: checked ? '20px' : '2px',
        width: '18px', height: '18px', borderRadius: '50%', background: '#fff',
        transition: 'left 0.15s',
      }} />
    </div>
  );
}

function SettingsPage() {
  const [loggingEnabled, setLoggingEnabled] = useState<boolean>(true);
  const [loading, setLoading] = useState<boolean>(true);
  const [confirmLogout, setConfirmLogout] = useState<boolean>(false);
  const [loggingOut, setLoggingOut] = useState<boolean>(false);
  const [serverInfo, setServerInfo] = useState<string>('');

  useEffect(() => { (async () => {
    // The "RomM" tile is mandatory and auto-created at plugin load. Reconcile
    // here too (the shortcut store is reliably ready by the time Settings opens)
    // to sweep duplicates and repair the survivor's exe/name/art after updates.
    try { await reconcileRommTile(); } catch { /* ignore */ }
    try {
      const cfg = await getConfig();
      const url = cfg?.url || '';
      // Prefer the live RomM account name; never show the stored credential/token.
      let name = '';
      try { name = (await getAccountUsername())?.username || ''; } catch { /* ignore */ }
      setServerInfo(name && url ? `${name} · ${url}` : (name || url || ''));
    } catch { /* ignore */ }
  })(); }, []);

  useEffect(() => {
    // Load initial logging preference
    const loadSettings = async () => {
      try {
        const enabled = await getLoggingEnabled();
        setLoggingEnabled(enabled);
      } catch (error) {
        console.error('Failed to load logging preference:', error);
      } finally {
        setLoading(false);
      }
    };
    loadSettings();
  }, []);

  const handleLoggingToggle = async (enabled: boolean) => {
    setLoggingEnabled(enabled);
    try {
      await updateLoggingEnabled(enabled);
    } catch (error) {
      console.error('Failed to set logging preference:', error);
      setLoggingEnabled(!enabled);
    }
  };

  const handleLogout = async (wipeData: boolean) => {
    setLoggingOut(true);
    try {
      const result = await logout(wipeData);
      if (result?.success) {
        toaster.toast({
          title: 'Logged out',
          body: wipeData
            ? `${result.deleted_roms ?? 0} ROM file(s) deleted. Signed out of RomM.`
            : 'Signed out of RomM. Downloaded files were kept.',
        });
        // Hand back to the setup wizard so the user can sign in again.
        Navigation.Navigate("/romm-sync-setup");
        Navigation.CloseSideMenus();
      } else {
        toaster.toast({ title: 'Logout failed', body: result?.error ?? 'Unknown error' });
      }
    } catch (error) {
      toaster.toast({ title: 'Logout failed', body: String(error) });
    } finally {
      setLoggingOut(false);
      setConfirmLogout(false);
    }
  };

  return v2Page(
    <Focusable noFocusRing
      onButtonDown={(e: any) => { if (e?.detail?.button === GamepadButton.CANCEL) Navigation.NavigateBack(); }}
      style={{ maxWidth: '760px', margin: '0 auto', padding: '20px 20px 0' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '24px' }}>
        <GameActionButton icon={<FaChevronLeft size={16} />} onClick={() => Navigation.NavigateBack()} />
        <div style={{ fontSize: '24px', fontWeight: 800, letterSpacing: '-0.01em' }}>Settings</div>
      </div>

      <V2SettingsSection title="Debug">
        <V2SettingsRow
          icon={<FaBug size={16} />}
          title="Enable debug logging"
          subtitle="Write logs to ~/.config/romm-retroarch-sync/decky_debug.log"
          onClick={loading ? undefined : () => handleLoggingToggle(!loggingEnabled)}
          right={<V2Switch checked={loggingEnabled} />}
          disabled={loading}
        />
      </V2SettingsSection>

      <V2SettingsSection title="Account">
        {!confirmLogout ? (
          <V2SettingsRow
            icon={<FaUndo size={16} />}
            title="Log out"
            subtitle={serverInfo ? `Signed in as ${serverInfo}` : 'Sign out and return to setup.'}
            onClick={() => setConfirmLogout(true)}
            danger
          />
        ) : (
          <div style={{
            display: 'flex', flexDirection: 'column', gap: '12px', padding: '16px',
            borderRadius: V2.radiusCard, background: 'rgba(255,80,80,0.08)',
            border: `1px solid rgba(255,80,80,0.40)`,
          }}>
            <div style={{ fontSize: '13px', color: V2.fg2, lineHeight: 1.4 }}>
              How do you want to log out? You'll return to the setup wizard either way.
            </div>
            <GameActionButton icon={<FaUndo size={14} />} label={loggingOut ? 'Logging out…' : 'Log out (keep downloads)'}
              onClick={() => handleLogout(false)} disabled={loggingOut} />
            <GameActionButton icon={<FaTrash size={14} />} label={loggingOut ? 'Logging out…' : 'Log out & delete all downloads'}
              variant="danger" onClick={() => handleLogout(true)} disabled={loggingOut} />
            <GameActionButton icon={<FaTimes size={14} />} label="Cancel"
              onClick={() => setConfirmLogout(false)} disabled={loggingOut} />
          </div>
        )}
      </V2SettingsSection>

      <V2SettingsSection title="About">
        <div style={{
          display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '8px',
          padding: '20px 16px', borderRadius: V2.radiusCard, background: V2.surface,
          border: `1px solid ${V2.border}`,
        }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px' }}>
            <span style={{ fontWeight: 800, fontSize: '16px' }}>RomM RetroArch Sync</span>
            <span style={{ color: V2.fgMuted, fontSize: '12px' }}>v1.6.0</span>
          </div>
          <div style={{ color: V2.fgMuted, fontSize: '12px' }}>by Covin</div>
          <div style={{ display: 'flex', gap: '10px', marginTop: '6px' }}>
            {[
              { icon: <FaGithub size={13} />, label: 'GitHub', url: 'https://github.com/Covin90/romm-retroarch-sync' },
              { icon: <FaBug size={13} />, label: 'Report Issue', url: 'https://github.com/Covin90/romm-retroarch-sync/issues' },
            ].map(({ icon, label, url }) => (
              <V2Button key={label} variant="tonal" onClick={() => Navigation.NavigateToExternalWeb(url)}>
                {icon}<span>{label}</span>
              </V2Button>
            ))}
          </div>
        </div>
      </V2SettingsSection>
    </Focusable>
  );
}

function Content() {
  // Slim QAM launcher: connection status + entry into the full-screen app.
  // All management (collections, BIOS, settings) now lives in-app — open it
  // with the gear here, or press Select anywhere in the browser.
  const [status, setStatus] = useState<any>({ status: 'loading', message: 'Loading…' });
  const [configured, setConfigured] = useState<boolean | null>(null);
  const configuredRef = useRef<boolean | null>(null);
  const intervalRef = useRef<any>(null);

  const getStatusColor = () => {
    switch (status.status) {
      case 'connected': return '#4ade80';
      case 'running': return '#fbbf24';
      case 'stopped': return '#f87171';
      case 'error': return '#f87171';
      default: return '#9ca3af';
    }
  };

  const checkConfigured = async () => {
    try {
      const cfg = await getConfig();
      const isConfigured = cfg?.configured ?? false;
      configuredRef.current = isConfigured;
      setConfigured(isConfigured);
      return isConfigured;
    } catch {
      setConfigured(false);
      return false;
    }
  };

  const refreshStatus = async () => {
    try {
      if (configuredRef.current === false) {
        if (!(await checkConfigured())) return;
      }
      setStatus(await getServiceStatus());
    } catch {
      setStatus({ status: 'error', message: '❌ Plugin error' });
    }
  };

  useEffect(() => {
    checkConfigured().then(refreshStatus);
    intervalRef.current = setInterval(refreshStatus, 2000);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, []);

  if (configured === null) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <div style={{ color: '#9ca3af', fontSize: '0.9em' }}>Loading…</div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (configured === false) {
    return (
      <PanelSection title="RomM Sync">
        <PanelSectionRow>
          <div style={{ fontSize: '0.85em', color: '#d1d5db', lineHeight: '1.5' }}>
            Connect your SteamOS device to your RomM server to sync ROMs and saves.
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => { Navigation.Navigate("/romm-sync-setup"); Navigation.CloseSideMenus(); }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <FaCog size={14} />
              <span>Get Started</span>
            </div>
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <PanelSection>
      <PanelSectionRow>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.85em', padding: '4px 0' }}>
          <div style={{ width: '8px', height: '8px', borderRadius: '50%', backgroundColor: getStatusColor(), flexShrink: 0 }} />
          <span>{(status.message || '').replace(', ', ' - ')}</span>
        </div>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => { Navigation.Navigate("/romm-sync-library"); Navigation.CloseSideMenus(); }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <FaGamepad size={14} />
            <span>Open RomM</span>
          </div>
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => { Navigation.Navigate("/romm-sync-settings"); Navigation.CloseSideMenus(); }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <FaCog size={14} />
            <span>Settings</span>
          </div>
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <div style={{ fontSize: '11px', color: '#9ca3af', padding: '4px 2px 0', lineHeight: 1.4 }}>
          Tip: press Select in the browser to open Settings.
        </div>
      </PanelSectionRow>
    </PanelSection>
  );
}

function TitleView() {
  const [isRefreshing, setIsRefreshing] = useState(false);

  const handleRefresh = async () => {
    if (isRefreshing) return;
    setIsRefreshing(true);
    try {
      // Call refresh_from_romm to fetch fresh data from server
      const result = await refreshFromRomm(false); // false = incremental refresh
      if (result?.success) {
        console.log('[REFRESH] Successfully refreshed from RomM:', result.message);
      } else {
        console.warn('[REFRESH] Refresh returned non-success:', result?.message);
      }
      // Keep spinning for at least 500ms for visual feedback
      setTimeout(() => setIsRefreshing(false), 500);
    } catch (error) {
      console.error('[REFRESH] Failed to refresh from RomM:', error);
      setIsRefreshing(false);
    }
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', width: '100%' }}>
      <div style={{ marginRight: 'auto', flex: 0.9 }}>RomM Sync</div>
      <DialogButton
        style={{ height: '28px', width: '28px', minWidth: 0, padding: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', marginRight: '4px' }}
        onClick={handleRefresh}
        disabled={isRefreshing}
      >
        <FaSync style={{
          display: 'block',
          animation: isRefreshing ? 'spin 1s linear infinite' : 'none'
        }} />
      </DialogButton>
      <DialogButton
        style={{ height: '28px', width: '28px', minWidth: 0, padding: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
        onClick={() => {
          Navigation.Navigate("/romm-sync-settings");
          Navigation.CloseSideMenus();
        }}
      >
        <BsGearFill style={{ display: 'block' }} />
      </DialogButton>
      <style>{`
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}

// ─── Setup wizard ────────────────────────────────────────────────────────────
// Full-screen guided first-run flow (RomM v2 visual language): Welcome →
// Connect (login or pair code, with Test) → Folders → Finish. Auto-opened on
// startup when no connection is configured; also reachable from the QAM.

// RomSwitch — React port of RomM's frontend/src/v2/lib/forms/RSwitch/RSwitch.vue.
// An iOS-style 36×20px track with a 14px knob that slides on toggle, with the
// brand-purple background, inner sheen, outer glow, spring easing and active
// press squash that define the RomM v2 toggle's feel.
function RomSwitch({ checked, onChange, disabled, label, description }:
  { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string; description?: string }) {
  const row = (inner: any) => (
    <div
      onClick={() => { if (!disabled) onChange(!checked); }}
      style={{
        display: 'flex', alignItems: 'center', gap: '14px', width: '100%',
        padding: '12px 14px', borderRadius: V2.radiusMd, cursor: disabled ? 'not-allowed' : 'pointer',
        background: 'rgba(255,255,255,0.045)', border: `1px solid ${V2.border}`,
        opacity: disabled ? 0.55 : 1, transition: 'background 0.2s, border-color 0.2s',
      }}
    >
      <div className={`r-switch${checked ? ' r-switch--on' : ''}${disabled ? ' r-switch--disabled' : ''}`} style={{ display: 'inline-flex', alignItems: 'center', background: 'transparent', border: 'none', padding: 0 }}>
        <span className="r-switch__track" style={{ position: 'relative', flexShrink: 0, borderRadius: '999px', background: checked ? V2.brand : V2.borderStrong, overflow: 'hidden', width: '36px', height: '20px', transition: 'background 260ms cubic-bezier(0.45,0.05,0.55,0.95), box-shadow 260ms cubic-bezier(0.45,0.05,0.55,0.95)' }}>
          <span className="r-switch__knob" style={{ position: 'absolute', top: '3px', left: '3px', borderRadius: '50%', background: checked ? '#111117' : V2.fg, width: '14px', height: '14px', transform: checked ? 'translateX(16px) scaleX(1)' : 'translateX(0) scaleX(1)', transformOrigin: checked ? 'right center' : 'left center', transition: 'transform 340ms cubic-bezier(0.34,1.56,0.64,1), background 200ms cubic-bezier(0.22,1,0.36,1)', boxShadow: '0 1px 2px rgba(0,0,0,0.22)' }} />
        </span>
      </div>
      {(label || description) && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '2px', flex: '1 1 auto', minWidth: 0 }}>
          {label && <span style={{ fontSize: '14px', fontWeight: 500, color: V2.fg }}>{label}</span>}
          {description && <span style={{ fontSize: '12px', color: V2.fgMuted, lineHeight: 1.45 }}>{description}</span>}
        </div>
      )}
      {inner}
    </div>
  );
  // The scoped <style> block carries the box-shadow sheen/glow + hover halo +
  // active-press squash that can't be expressed as inline styles.
  return (
    <>
      <style>{`
        .r-switch--on .r-switch__track{box-shadow:inset 0 1px 0 rgba(255,255,255,0.18),0 0 12px rgba(139,116,232,0.38)}
        .r-switch:not(.r-switch--disabled){cursor:pointer}
        .r-switch--disabled{cursor:not-allowed;opacity:0.55}
        .r-switch > div:hover .r-switch__knob,.r-switch:hover:not(.r-switch--disabled) .r-switch__knob{box-shadow:0 2px 4px rgba(0,0,0,0.28),0 0 0 5px rgba(255,255,255,0.10)}
        .r-switch--on:hover .r-switch__knob,.r-switch--on:hover:not(.r-switch--disabled) .r-switch__knob{box-shadow:0 2px 4px rgba(0,0,0,0.28),0 0 0 5px rgba(139,116,232,0.22)}
        .r-switch:active:not(.r-switch--disabled) .r-switch__knob{transform:translateX(0) scaleX(1.35);transition:transform 110ms cubic-bezier(0.22,1,0.36,1)}
        .r-switch--on:active:not(.r-switch--disabled) .r-switch__knob{transform:translateX(16px) scaleX(1.35);transition:transform 110ms cubic-bezier(0.22,1,0.36,1)}
        @media(prefers-reduced-motion:reduce){.r-switch__track,.r-switch__knob{transition:none!important}.r-switch:active .r-switch__knob{transform:translateX(0) scaleX(1)!important}.r-switch--on:active .r-switch__knob{transform:translateX(16px) scaleX(1)!important}}
      `}</style>
      {row(null)}
    </>
  );
}

function SetupWizard() {
  const [step, setStep] = useState(0);
  const [mode, setMode] = useState<'login' | 'pair'>('pair');
  const [logo, setLogo] = useState<string | null>(null);
  const [url, setUrl] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [pairCode, setPairCode] = useState('');
  const [romDir, setRomDir] = useState('');
  const [saveDir, setSaveDir] = useState('');
  const [biosDir, setBiosDir] = useState('');
  const [deviceName, setDeviceName] = useState('');
  const [deviceNameDefault, setDeviceNameDefault] = useState('SteamOS');
  const [hasPassword, setHasPassword] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const c = await getConfig();
        setUrl(c.url || ''); setUsername(c.username || '');
        setRomDir(c.rom_directory || ''); setSaveDir(c.save_directory || ''); setBiosDir(c.bios_directory || '');
        setDeviceName(c.device_name || ''); setDeviceNameDefault(c.device_name_default || 'SteamOS');
        setHasPassword(c.has_password || false);
      } catch { /* ignore */ }
      try { const l = await getRommLogo(); setLogo(l?.data_uri || null); } catch { /* ignore */ }
    })();
  }, []);

  const finish = () => {
    // Pop the full-screen wizard route off the history stack first, otherwise
    // pressing Back from the library lands the user right back in the wizard.
    try { Navigation.NavigateBack(); } catch { /* ignore */ }
    setTimeout(() => { Navigation.Navigate("/romm-sync-library"); Navigation.CloseSideMenus(); }, 60);
  };

  const doTest = async () => {
    setTesting(true); setTestResult(null);
    try { setTestResult(await testRommConnection(url.trim(), username.trim(), password)); }
    catch { setTestResult({ success: false, message: 'Test failed unexpectedly.' }); }
    finally { setTesting(false); }
  };

  const doFinish = async () => {
    setBusy(true);
    try {
      // The "RomM" Steam library tile is mandatory — ensure it exists before
      // navigating away (create if missing; reconcile repairs an existing one).
      try {
        if ((await reconcileRommTile()) == null) await addRommShortcut();
      } catch (e) { console.error('[RomM] wizard steam tile', e); }

      if (mode === 'pair') {
        const r = await pairDevice(url.trim(), pairCode.trim());
        if (r?.success) { toaster.toast({ title: 'RomM Sync', body: 'Paired — connecting…' }); finish(); }
        else toaster.toast({ title: 'RomM Sync', body: r?.message || 'Pairing failed.' });
      } else {
        const dev = deviceName.trim() || deviceNameDefault;
        const r = await saveConfig(url.trim(), username.trim(), password, romDir.trim(), saveDir.trim(), dev, biosDir.trim());
        if (r?.success) { toaster.toast({ title: 'RomM Sync', body: 'Connected!' }); finish(); }
        else toaster.toast({ title: 'RomM Sync', body: r?.error || 'Failed to save configuration.' });
      }
    } catch { toaster.toast({ title: 'RomM Sync', body: 'Something went wrong.' }); }
    finally { setBusy(false); }
  };

  const TOTAL = 4;
  const next = () => setStep((s) => Math.min(s + 1, TOTAL - 1));
  const back = () => setStep((s) => Math.max(s - 1, 0));
  const canConnect = mode === 'pair'
    ? (url.trim() && pairCode.trim())
    : (url.trim() && username.trim() && (password.length > 0 || hasPassword));

  const onField = (set: (v: string) => void, isPair = false) => (v: string) => { set(v); if (!isPair) setTestResult(null); };
  // Pair codes follow XXXX-XXXX — strip junk, uppercase, auto-insert the dash.
  const formatPairCode = (raw: string) => {
    const clean = (raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8);
    return clean.length > 4 ? `${clean.slice(0, 4)}-${clean.slice(4)}` : clean;
  };
  const browse = (cur: string, set: (v: string) => void) => async () => {
    try { const res = await openFilePicker(FileSelectionType.FOLDER, cur || '/home/deck', false, true); if (res?.realpath) set(res.realpath); }
    catch { /* ignore */ }
  };

  // Footer: Back pinned left, Next/primary pinned right (space-between).
  const footer = (primary: any) => (
    <Focusable noFocusRing flow-children="horizontal" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%', marginTop: '8px' }}>
      <GameActionButton variant="surface" label="Back" icon={<FaChevronLeft size={13} />} onClick={back} />
      {primary}
    </Focusable>
  );

  return (
    <div style={{
      position: 'fixed', inset: 0, color: V2.fg, fontFamily: V2.font,
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      padding: '40px 24px', overflowY: 'auto',
    }}>
      <V2Bg uri={null} />
      <style>{`
        @keyframes wizIn { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: none; } }
        @keyframes wizPop { 0% { opacity: 0; transform: scale(0.6); } 60% { transform: scale(1.08); } 100% { opacity: 1; transform: scale(1); } }
        @keyframes wizDot { from { transform: scale(0.6); } to { transform: scale(1); } }
        .wiz-step > div > * { animation: wizIn 0.5s cubic-bezier(.22,1,.36,1) both; }
        .wiz-step > div > *:nth-child(1) { animation-delay: 0.02s; }
        .wiz-step > div > *:nth-child(2) { animation-delay: 0.07s; }
        .wiz-step > div > *:nth-child(3) { animation-delay: 0.12s; }
        .wiz-step > div > *:nth-child(4) { animation-delay: 0.17s; }
        .wiz-step > div > *:nth-child(5) { animation-delay: 0.22s; }
        .wiz-step > div > *:nth-child(6) { animation-delay: 0.27s; }
        .wiz-step > div > *:nth-child(n+7) { animation-delay: 0.32s; }
        .wiz-logo { animation: wizPop 0.55s cubic-bezier(.22,1,.36,1) both !important; filter: drop-shadow(0 6px 24px rgba(139,116,232,0.45)); }
        .wiz-check { animation: wizPop 0.6s cubic-bezier(.34,1.56,.64,1) both !important; }
        .wiz-dot { transition: width 0.32s cubic-bezier(.22,1,.36,1), background 0.32s ease; }
        .wiz-dot--active { animation: wizDot 0.32s ease; }
      `}</style>
      <Focusable noFocusRing style={{
        position: 'relative', zIndex: 2, width: '100%', maxWidth: '440px',
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '22px', textAlign: 'center',
      }}>
        {/* Progress dots */}
        <div style={{ display: 'flex', justifyContent: 'center', gap: '8px' }}>
          {Array.from({ length: TOTAL }).map((_, i) => (
            <div key={i} className={`wiz-dot${i === step ? ' wiz-dot--active' : ''}`} style={{
              width: i === step ? '22px' : '8px', height: '8px', borderRadius: V2.radiusPill,
              background: i <= step ? V2.brand : V2.surfaceHover,
            }} />
          ))}
        </div>

        <div key={step} className="wiz-step" style={{ width: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '22px' }}>
        {step === 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px', textAlign: 'center' }}>
            {logo && <img className="wiz-logo" src={logo} style={{ width: '96px', height: '96px', objectFit: 'contain' }} />}
            <div style={{ fontSize: '28px', fontWeight: 800, letterSpacing: '-0.01em' }}>Welcome to RomM Sync</div>
            <div style={{ fontSize: '14px', color: V2.fg2, lineHeight: 1.6, maxWidth: '420px' }}>
              Connect this device to your RomM server to browse your library, download games, and sync saves across devices.
            </div>
            <div style={{ marginTop: '8px' }}>
              <GameActionButton variant="emphasized" label="Get started" icon={<FaPlay size={13} style={{ marginLeft: '2px' }} />} onClick={next} />
            </div>
          </div>
        )}

        {step === 1 && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '14px', width: '100%' }}>
            <div style={{ fontSize: '20px', fontWeight: 700 }}>Connect to RomM</div>
            {/* Login / Pair toggle — emphasized when active, surface when not. */}
            <Focusable noFocusRing flow-children="horizontal" style={{ display: 'flex', justifyContent: 'center', gap: '8px' }}>
              {(['login', 'pair'] as const).map((m) => (
                <GameActionButton key={m} variant={mode === m ? 'emphasized' : 'surface'}
                  label={m === 'login' ? 'Username & password' : 'Pair code'} icon={null}
                  onClick={() => { setMode(m); setTestResult(null); }} />
              ))}
            </Focusable>
            <V2TextField label="RomM URL" value={url} onChange={onField(setUrl)} placeholder="https://romm.example.com" />
            {mode === 'login' ? (
              <>
                <V2TextField label="Username" value={username} onChange={onField(setUsername)} />
                <V2TextField label="Password" value={password} onChange={onField(setPassword)} password
                  placeholder={hasPassword && !password ? 'Leave blank to keep saved' : undefined} />
                {testResult && (
                  <div style={{ fontSize: '13px', color: testResult.success ? V2.success : V2.danger }}>
                    {testResult.success ? '✅' : '❌'} {testResult.message}
                  </div>
                )}
                <GameActionButton variant="surface" label={testing ? 'Testing…' : 'Test connection'} icon={null}
                  disabled={testing || !url.trim() || !username.trim()} onClick={doTest} />
              </>
            ) : (
              <>
                <PairCodeField label="Pairing code" value={pairCode}
                  onChange={(v) => setPairCode(formatPairCode(v))} />
              </>
            )}
            {footer(
              <GameActionButton variant="emphasized" label="Next" icon={<FaChevronRight size={13} />} disabled={!canConnect} onClick={next} />
            )}
          </div>
        )}

        {step === 2 && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '12px', width: '100%' }}>
            <div style={{ fontSize: '20px', fontWeight: 700 }}>Folders</div>
            <div style={{ fontSize: '13px', color: V2.fg2, lineHeight: 1.6, maxWidth: '420px' }}>
              <div>Where ROMs, saves and BIOS files live on this device.</div>
            </div>
            <V2TextField label="ROM directory" value={romDir} onChange={setRomDir} />
            <GameActionButton variant="surface" label="Browse…" icon={null} onClick={browse(romDir, setRomDir)} />
            <V2TextField label="Save directory" value={saveDir} onChange={setSaveDir} />
            <GameActionButton variant="surface" label="Browse…" icon={null} onClick={browse(saveDir, setSaveDir)} />
            <V2TextField label="BIOS directory" value={biosDir} onChange={setBiosDir} />
            <GameActionButton variant="surface" label="Browse…" icon={null} onClick={browse(biosDir, setBiosDir)} />
            <V2TextField label="Device name" value={deviceName} onChange={setDeviceName} placeholder={deviceNameDefault} />
            {footer(
              <GameActionButton variant="emphasized" label="Next" icon={<FaChevronRight size={13} />} onClick={next} />
            )}
          </div>
        )}

        {step === 3 && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px', textAlign: 'center', width: '100%' }}>
            <div className="wiz-check"><FaCheckCircle size={56} color={V2.success} /></div>
            <div style={{ fontSize: '24px', fontWeight: 800 }}>Ready to go</div>
            <div style={{ fontSize: '14px', color: V2.fg2, lineHeight: 1.6, maxWidth: '420px' }}>
              {mode === 'pair'
                ? 'We\'ll pair this device with your RomM server and open your library.'
                : 'We\'ll save your connection and open your library.'}
            </div>
            {footer(
              <GameActionButton variant="emphasized" disabled={busy}
                label={busy ? 'Connecting…' : 'Finish & open library'}
                icon={busy ? <FaSync size={14} style={{ animation: 'spin 1s linear infinite' }} /> : <FaPlay size={13} style={{ marginLeft: '2px' }} />}
                onClick={doFinish} />
            )}
          </div>
        )}
        </div>
      </Focusable>
    </div>
  );
}

// ─── Steam library "RomM" shortcut (mandatory) ───────────────────────────────
// A non-Steam shortcut named "RomM" is auto-created at plugin load (once RomM is
// configured) and is required: launching it from the library opens the Game
// Browser, and picking a game RunGame's this same tile so the emulator runs as a
// Steam-tracked child (working overlay). All SteamClient calls are undocumented +
// version-fragile, so every call is feature-detected and wrapped — failures
// degrade to a toast, never a crash.
const ROMM_SHORTCUT_NAME = "RomM";
// Fallback exe when the session-host script can't be resolved. With /bin/true the
// tile still opens the browser (via the launch intercept) but the Steam-overlay
// session-host launch path is unavailable, so Play falls back to a direct launch.
const ROMM_SHORTCUT_EXE = "/bin/true";
// The real exe: bin/romm-session-host. When the tile is RunGame'd after a game is
// picked, Steam launches this as a tracked game (opening the overlay session) and
// it execs the resolved emulator argv in-place — so the emulator inherits the
// overlay. Resolved lazily from the backend (absolute, plugin-dir dependent).
let _sessionHostExe: string | null = null;
async function rommShortcutExe(): Promise<string> {
  if (_sessionHostExe) return _sessionHostExe;
  try {
    const r = await getSessionHostPath();
    const p = typeof r === 'string' ? r : r?.path;
    if (p) { _sessionHostExe = p; return p; }
  } catch (e) { console.error('[RomM] getSessionHostPath', e); }
  return ROMM_SHORTCUT_EXE;
}
// Stamped into the shortcut's launch options so we can re-identify our tile
// even when Steam hasn't persisted its name (the root cause of duplicates and
// the toggle flipping off after an update).
const ROMM_TILE_SENTINEL = "romm-sync-monitor-tile";
let _rommAppId: number | null = null;
let _rommNavTimer: any = null;
let _rommActionReg: { unregister: () => void } | null = null;
// Set true immediately before we RunGame the tile to launch a *picked* game, so
// the launch intercept lets the session-host run (and execs the emulator) instead
// of treating it as a bare tile click (terminate + open the browser).
let _rommLaunchPending = false;
// Set true while a *picked-game* session is running (host execs the emulator as a
// Steam-tracked child). When that session ends we want to return to the Game
// Browser, not leave the user dropped on the Steam/Big-Picture library.
let _rommSessionActive = false;
let _rommLifetimeReg: { unregister: () => void } | null = null;
// Guards the startup setup-wizard auto-open so it fires at most once per session.
let _setupAutoOpened = false;

const _sc = (): any => (typeof window !== 'undefined' ? (window as any).SteamClient : undefined);

// Enumerate all non-Steam shortcut overviews. This build does NOT expose
// SteamClient.Apps.GetAllShortcuts, so we read the app stores Steam keeps in
// memory and keep only entries that report themselves as shortcuts.
// AppIds of all non-Steam shortcuts. deckDesktopApps.apps is a Map keyed by
// appid on this build; the map values are not overviews, so we resolve each
// overview separately via appStore.GetAppOverviewByAppID.
function _shortcutAppIds(): number[] {
  const m = (window as any).collectionStore?.deckDesktopApps?.apps;
  try { if (m?.keys) return Array.from(m.keys()).map((k: any) => Number(k)); } catch { /* ignore */ }
  return [];
}

function _appName(appid: number): string {
  try { return String((window as any).appStore?.GetAppOverviewByAppID?.(appid)?.display_name ?? ''); } catch { return ''; }
}

// All our tiles carry the name "RomM"; also accept the exe-derived fallback
// names Steam uses when a name didn't persist. Real games carry their own names.
function _isRommName(nm: string): boolean {
  return nm === ROMM_SHORTCUT_NAME || nm === 'true' || nm === '/bin/true'
    || nm === 'romm-session-host';
}

function _rommAppIds(): number[] {
  return _shortcutAppIds().filter((aid) => _isRommName(_appName(aid)));
}

async function findRommShortcut(): Promise<number | null> {
  if (_rommAppId != null) return _rommAppId;
  try {
    const ids = _rommAppIds();
    if (ids.length) return ids[0];
  } catch (e) { console.error('[RomM] findRommShortcut', e); }
  return null;
}

// Remove duplicate RomM tiles left behind by earlier sessions, keeping one.
// Returns the surviving appId (or null if none).
async function cleanupRommShortcuts(): Promise<number | null> {
  try {
    const apps = _sc()?.Apps;
    const mine = _rommAppIds();
    if (mine.length === 0) return null;
    const keep = mine[0];
    for (const aid of mine.slice(1)) {
      try { await apps?.RemoveShortcut?.(aid); } catch (e) { console.error('[RomM] dedup remove', e); }
    }
    return keep;
  } catch (e) { console.error('[RomM] cleanupRommShortcuts', e); return null; }
}

// Bring the RomM tile into a known-good state: collapse duplicates to one,
// repair the survivor's name + sentinel, repaint art, and bind the launch
// intercept. Safe to call repeatedly. Returns the surviving appId (or null).
// Run this when the shortcut store is ready (e.g. on Settings open), not only
// at plugin load, where GetAllShortcuts can still be empty.
async function reconcileRommTile(): Promise<number | null> {
  const appId = (await cleanupRommShortcuts()) ?? (await findRommShortcut());
  if (appId == null) return null;
  _rommAppId = appId;
  const apps = _sc()?.Apps;
  try { await apps?.SetShortcutName?.(appId, ROMM_SHORTCUT_NAME); } catch { /* ignore */ }
  try { await apps?.SetShortcutLaunchOptions?.(appId, ROMM_TILE_SENTINEL); } catch { /* ignore */ }
  // Migrate the exe to the session-host script (older tiles used /bin/true).
  try {
    const exe = await rommShortcutExe();
    if (apps?.SetShortcutExe) await apps.SetShortcutExe(appId, exe);
  } catch (e) { console.error('[RomM] reconcile SetShortcutExe', e); }
  // Stamping name/launch-options can renumber the shortcut appid (it's a hash of
  // exe+name+options). Re-resolve so the cache, intercept and artwork all target
  // the live tile rather than the now-dead pre-stamp appid.
  const liveId = (_rommAppIds()[0]) ?? appId;
  _rommAppId = liveId;
  registerRommLaunchIntercept();
  ensureRommArtwork(liveId);
  return liveId;
}

async function ensureRommArtwork(appId: number) {
  try {
    const apps = _sc()?.Apps;
    if (!apps?.SetCustomArtworkForApp) return;
    // Steam asset types -> files this build writes:
    //   0 -> {appid}p.png   (portrait capsule)  : grid
    //   1 -> {appid}_hero   (hero background)    : hero
    //   2 -> {appid}_logo   (transparent logo)   : logo
    //   4 -> {appid}.png    (landscape capsule)  : THIS is the image Big
    //        Picture's "Recent Games" featured banner uses, so it must get the
    //        background-only landscape art, not the centered-mark icon.
    // (Type 3 is a no-op on this build, so the landscape goes through type 4.)
    const res = await getRommArtwork();
    const art = res?.art as Record<string, string> | undefined;
    if (art && Object.keys(art).length) {
      const ext = res.ext || 'png';
      // type -> source art key. Type 4 (the landscape {appid}.png) is fed the
      // header (bg-only) art instead of the icon.
      const plan: [number, string][] = [[0, '0'], [1, '1'], [2, '2'], [4, '3']];
      for (const [n, key] of plan) {
        if (!art[key]) continue;
        // Clear first: Steam won't overwrite an existing custom asset, so a
        // repaint over stale art would otherwise silently no-op.
        try { await apps.ClearCustomArtworkForApp?.(appId, n); } catch { /* ignore */ }
        try { await apps.SetCustomArtworkForApp(appId, art[key], ext, n); } catch { /* ignore */ }
      }
      return;
    }
    // Fallback to the flat logo if branded artwork is unavailable.
    const logo = await getPluginLogo();
    if (!logo?.b64) return;
    try { await apps.SetCustomArtworkForApp(appId, logo.b64, logo.ext || 'png', 0); } catch { /* ignore */ }
    try { await apps.SetCustomArtworkForApp(appId, logo.b64, logo.ext || 'png', 4); } catch { /* ignore */ }
  } catch (e) { console.error('[RomM] ensureRommArtwork', e); }
}

// Create the shortcut if missing; returns the appId (or null on failure).
async function addRommShortcut(): Promise<number | null> {
  try {
    const apps = _sc()?.Apps;
    if (!apps?.AddShortcut) { toaster.toast({ title: 'RomM', body: 'Steam shortcuts API unavailable on this build.' }); return null; }
    const exe = await rommShortcutExe();
    let appId = await findRommShortcut();
    if (appId == null) {
      appId = Number(await apps.AddShortcut(ROMM_SHORTCUT_NAME, exe, "", ""));
    } else if (apps.SetShortcutExe) {
      // Migrate older /bin/true tiles to the session-host exe so the overlay
      // launch path works. Harmless if already set.
      try { await apps.SetShortcutExe(appId, exe); } catch (e) { console.error('[RomM] SetShortcutExe', e); }
    }
    // Always (re)assert the display name. AddShortcut's name arg doesn't reliably
    // persist to the shortcut's strAppName on all Steam builds, so the tile can
    // show up blank/"true" without an explicit SetShortcutName.
    if (apps.SetShortcutName) {
      try { await apps.SetShortcutName(appId, ROMM_SHORTCUT_NAME); } catch (e) { console.error('[RomM] SetShortcutName', e); }
    }
    // Stamp the sentinel so we can always re-find this tile even if the name
    // doesn't persist — this is what prevents duplicate tiles piling up.
    if (apps.SetShortcutLaunchOptions) {
      try { await apps.SetShortcutLaunchOptions(appId, ROMM_TILE_SENTINEL); } catch (e) { console.error('[RomM] SetShortcutLaunchOptions', e); }
    }
    _rommAppId = appId;
    await ensureRommArtwork(appId);
    registerRommLaunchIntercept();
    registerRommSessionEndWatch();
    return appId;
  } catch (e) {
    console.error('[RomM] addRommShortcut', e);
    toaster.toast({ title: 'RomM', body: 'Could not add the library tile.' });
    return null;
  }
}


// Intercept the RomM tile's launch → cancel the no-op run and open the browser.
function registerRommLaunchIntercept() {
  try {
    if (_rommActionReg) return;
    const apps = _sc()?.Apps;
    if (!apps?.RegisterForGameActionStart) return;
    _rommActionReg = apps.RegisterForGameActionStart((_actionType: number, strAppId: string) => {
      const raw = Number(strAppId);
      // GameActionStart may pass the full 64-bit gameID; the 32-bit appid is its
      // high dword. Try both the raw value and the extracted appid.
      const hi = Math.floor(raw / 4294967296);
      const aid = hi > 0 ? hi : raw;
      // Identify by name/overview, not numeric id: GameActionStart's appid
      // representation (signed/unsigned/gameid) doesn't reliably equal the
      // collectionStore key, and stamping renumbers the cached id. Name match
      // sidesteps all of that.
      let mine = aid === _rommAppId || raw === _rommAppId;
      if (!mine) { try { mine = _isRommName(_appName(aid)) || _isRommName(_appName(raw)); } catch { /* ignore */ } }
      if (!mine) { try { mine = _rommAppIds().includes(aid) || _rommAppIds().includes(raw); } catch { /* ignore */ } }
      if (mine) {
        _rommAppId = aid;
        // A picked-game launch: we wrote a launch-spec and RunGame'd the tile so
        // the session-host can exec the emulator as a Steam-tracked child (overlay
        // works). Let it run — do NOT terminate or navigate.
        if (_rommLaunchPending) {
          _rommLaunchPending = false;
          // Remember this is a live emulator session so the app-lifetime
          // listener can navigate back to the Game Browser when it quits
          // (instead of leaving the user on the Steam/Big-Picture library).
          _rommSessionActive = true;
          return;
        }
        // Bare tile click: the exe is a no-op without a fresh spec. The tile
        // fires this twice (launch start type=6, then exit type=7 ~1.5s later).
        // End the launch immediately and open the Game Browser instead.
        try { _sc()?.Apps?.TerminateApp?.(String(strAppId), false); } catch { /* ignore */ }
        if (_rommNavTimer != null) { try { clearTimeout(_rommNavTimer); } catch { /* ignore */ } }
        _rommNavTimer = setTimeout(() => {
          _rommNavTimer = null;
          try { Navigation.Navigate("/romm-sync-library"); Navigation.CloseSideMenus(); } catch (e) { console.error('[RomM] nav', e); }
        }, 0);
      }
    });
  } catch (e) { console.error('[RomM] registerRommLaunchIntercept', e); }
}

// When a picked-game emulator session ends, the session-host PID exits and Steam
// returns to the library (Big Picture) — not the plugin. Register for app
// lifetime notifications so that when OUR tile stops running after a real
// session, we navigate straight back to the Game Browser.
function registerRommSessionEndWatch() {
  try {
    if (_rommLifetimeReg) return;
    const gs = _sc()?.GameSessions;
    if (!gs?.RegisterForAppLifetimeNotifications) return;
    _rommLifetimeReg = gs.RegisterForAppLifetimeNotifications((data: any) => {
      try {
        if (data?.bRunning) return;            // only care about stop events
        if (!_rommSessionActive) return;       // not our emulator session
        const aid = Number(data?.unAppID);
        let mine = aid === _rommAppId;
        if (!mine) { try { mine = _rommAppIds().includes(aid); } catch { /* ignore */ } }
        if (!mine) { try { mine = _isRommName(_appName(aid)); } catch { /* ignore */ } }
        if (!mine) return;
        _rommSessionActive = false;
        if (_rommNavTimer != null) { try { clearTimeout(_rommNavTimer); } catch { /* ignore */ } }
        _rommNavTimer = setTimeout(() => {
          _rommNavTimer = null;
          try { Navigation.Navigate("/romm-sync-library"); Navigation.CloseSideMenus(); } catch (e) { console.error('[RomM] nav', e); }
        }, 0);
      } catch (e) { console.error('[RomM] sessionEnd', e); }
    });
  } catch (e) { console.error('[RomM] registerRommSessionEndWatch', e); }
}

export default definePlugin(() => {
  routerHook.addRoute("/romm-sync-setup", () => <SetupWizard />, { exact: true });
  routerHook.addRoute("/romm-sync-settings", () => <SettingsPage />, { exact: true });
  routerHook.addRoute("/romm-sync-config", () => <ConfigPage />, { exact: true });
  routerHook.addRoute("/romm-sync-library", () => <LibraryGroupsPage />, { exact: true });
  routerHook.addRoute("/romm-sync-library/:key", () => <LibraryGamesPage />, { exact: true });
  routerHook.addRoute("/romm-sync-game/:romId", () => <GameDetailPage />, { exact: true });

  // Register the launch intercept IMMEDIATELY — independent of tile
  // reconciliation.  The intercept checks the appid/name at fire time, so it
  // is safe to bind before the shortcut store is ready.  This fixes the bug
  // where clicking the RomM tile in Big Picture did nothing until the user
  // opened the Decky panel (which triggered reconcileRommTile → register…).
  registerRommLaunchIntercept();
  registerRommSessionEndWatch();

  // Re-bind the launch intercept if the RomM tile was added in a prior session,
  // and auto-open the setup wizard once when no connection is configured.
  (async () => {
    let configured = false;
    try { configured = !!(await getConfig())?.configured; } catch { /* ignore */ }
    if (configured) {
      try {
        // The tile is mandatory: ensure it EXISTS (create if missing), not just
        // reconcile. The shortcut store may not be ready this early, so retry
        // with exponential backoff until it is.
        const ensureTile = async (attempt: number) => {
          try {
            if ((await reconcileRommTile()) != null) return;   // found + repaired
            if ((await addRommShortcut()) != null) return;     // created
          } catch (e) { console.error('[RomM] ensure tile', e); }
          if (attempt < 5) {
            const delay = Math.min(4000 * Math.pow(1.5, attempt), 15000);
            setTimeout(() => { ensureTile(attempt + 1); }, delay);
          }
        };
        await ensureTile(0);
      } catch (e) { console.error('[RomM] shortcut ensure', e); }
    } else if (!_setupAutoOpened) {
      // No connection yet: auto-open the wizard once per session (the tile is
      // created on wizard finish). Never re-trap the user after setup.
      _setupAutoOpened = true;
      setTimeout(() => { try { Navigation.Navigate("/romm-sync-setup"); } catch (e) { console.error('[RomM] setup auto-open', e); } }, 1500);
    }
  })();

  return {
    name: "RomM Sync Monitor",
    titleView: <TitleView />,
    content: <Content />,
    icon: <FaSync />,
    onDismount: () => {
      console.log('[PLUGIN] onDismount - Stopping background monitoring');
      stopBackgroundMonitoring();
      try { _rommActionReg?.unregister(); } catch { /* ignore */ }
      _rommActionReg = null;
      try { _rommLifetimeReg?.unregister(); } catch { /* ignore */ }
      _rommLifetimeReg = null;

      routerHook.removeRoute("/romm-sync-setup");
      routerHook.removeRoute("/romm-sync-settings");
      routerHook.removeRoute("/romm-sync-config");
      routerHook.removeRoute("/romm-sync-library");
      routerHook.removeRoute("/romm-sync-library/:key");
      routerHook.removeRoute("/romm-sync-game/:romId");
    },
  };
});