import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  TextField,
  Navigation,
  staticClasses,
  DialogButton,
  Focusable,
  GamepadButton,
  showModal,
  ModalRoot,
} from "@decky/ui";
import { callable, definePlugin, toaster, routerHook, openFilePicker, FileSelectionType } from "@decky/api";
import { useState, useEffect, useRef, ChangeEvent } from "react";
import { FaSync, FaTrash, FaCog, FaSteam, FaGithub, FaBug, FaUndo, FaCopy, FaGamepad, FaBookmark, FaHome, FaSearch, FaTimes, FaDownload, FaPlay, FaInfoCircle, FaRegClock, FaLayerGroup, FaChevronLeft, FaChevronRight } from "react-icons/fa";
import { BsGearFill } from "react-icons/bs";

// Call backend methods
const getServiceStatus = callable<[], any>("get_service_status");
const refreshFromRomm = callable<[boolean], any>("refresh_from_romm");
const toggleCollectionSync = callable<[string, boolean], boolean>("toggle_collection_sync");
const deleteCollectionRoms = callable<[string], boolean>("delete_collection_roms");
const getLoggingEnabled = callable<[], boolean>("get_logging_enabled");
const updateLoggingEnabled = callable<[boolean], boolean>("set_logging_enabled");
const getConfig = callable<[], any>("get_config");
const resetAllSettings = callable<[], any>("reset_all_settings");
const saveConfig = callable<[string, string, string, string, string, string, string], any>("save_config");
const testRommConnection = callable<[string, string, string], any>("test_connection");
const pairDevice = callable<[string, string], any>("pair_device");
const toggleCollectionSteamSync = callable<[string, boolean], any>("toggle_collection_steam_sync");
const getSaveHistory = callable<[number], any>("get_save_history");
const getSaveScreenshot = callable<[number, number, string], any>("get_save_screenshot");
const restoreSaveVersion = callable<[number, number, string, boolean], any>("restore_save_version");
// Game Browser
const getLibraryGroups = callable<[string], any>("get_library_groups");
const getLibraryGames = callable<[string, string], any>("get_library_games");
const getGameCover = callable<[number, boolean], any>("get_game_cover");
const getImage = callable<[string], any>("get_image");
const searchGames = callable<[string], any>("search_games");
const getGameDetail = callable<[number], any>("get_game_detail");
const downloadGame = callable<[number], any>("download_game");
const deleteGame = callable<[number], any>("delete_game");
const launchGame = callable<[number], any>("launch_game");
const getHomeData = callable<[], any>("get_home_data");

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
}
interface LibGame { rom_id: number; name: string; platform: string | null; is_downloaded: boolean; has_cover: boolean; }

function fmtBytes(n: number | null | undefined): string {
  if (!n || isNaN(n as any)) return '';
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB']) {
    if (v < 1024) return u === 'B' ? `${v.toFixed(0)} ${u}` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v.toFixed(1)} TB`;
}

function fmtReleaseYear(ts: number | null | undefined): string {
  if (!ts) return '';
  try {
    // RomM stores first_release_date as a unix timestamp (seconds).
    const d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '';
    return String(d.getUTCFullYear());
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
  const [uri, setUri] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  useEffect(() => {
    let alive = true;
    if (!hasCover) { setDone(true); onLoaded?.(null); return; }
    (async () => {
      try {
        const r = await getGameCover(romId, large);
        if (alive) { setUri(r?.data_uri || null); onLoaded?.(r?.data_uri || null); }
      }
      catch { /* ignore */ }
      finally { if (alive) setDone(true); }
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

// A single library grid card: art-only with a centered, single-line label
// underneath (RomM's GameCard language). Focus/hover scales the art and paints
// the brand glow; activating it opens the game; gaining focus feeds the cover
// to the page's background art.
function GameTile({ game, onOpen, onActiveCover }:
  { game: LibGame; onOpen: (g: LibGame) => void; onActiveCover: (uri: string | null) => void }) {
  const uriRef = useRef<string | null>(null);
  const [focused, setFocused] = useState(false);
  const [dl, setDl] = useState(!!game.is_downloaded);
  const [busy, setBusy] = useState<null | 'download' | 'delete' | 'launch'>(null);
  const activate = () => { onActiveCover(uriRef.current); };

  // Button scheme (RomM GameActions): A = download (if absent) / launch (if
  // present); X = details; Y = delete. Mouse: overlay buttons mirror these.
  const doDownload = async () => {
    if (busy) return;
    setBusy('download');
    try {
      const r = await downloadGame(game.rom_id);
      if (r?.success) { setDl(true); toaster.toast({ title: 'Downloaded', body: game.name }); }
      else toaster.toast({ title: 'Download failed', body: r?.message || 'Error' });
    } catch (e) { toaster.toast({ title: 'Download failed', body: String(e) }); }
    finally { setBusy(null); }
  };
  const doLaunch = async () => {
    if (busy) return;
    setBusy('launch');
    try {
      const r = await launchGame(game.rom_id);
      if (r?.success) toaster.toast({ title: 'Launching', body: game.name });
      else toaster.toast({ title: 'Launch failed', body: r?.message || 'Error' });
    } catch (e) { toaster.toast({ title: 'Launch failed', body: String(e) }); }
    finally { setBusy(null); }
  };
  const doDelete = async () => {
    if (busy) return;
    setBusy('delete');
    try {
      const r = await deleteGame(game.rom_id);
      if (r?.success) { setDl(false); toaster.toast({ title: 'Deleted', body: game.name }); }
      else toaster.toast({ title: 'Delete failed', body: r?.message || 'Error' });
    } catch (e) { toaster.toast({ title: 'Delete failed', body: String(e) }); }
    finally { setBusy(null); }
  };
  const primary = () => { if (dl) doLaunch(); else doDownload(); };

  return (
    <Focusable
      onActivate={primary}
      onClick={primary}
      onSecondaryButton={() => onOpen(game)}
      onSecondaryActionDescription="Details"
      onOptionsButton={() => { if (dl) doDelete(); }}
      onOptionsActionDescription={dl ? 'Delete' : undefined}
      onOKActionDescription={dl ? 'Launch' : 'Download'}
      onFocus={() => { setFocused(true); activate(); }}
      onMouseEnter={() => { setFocused(true); activate(); }}
      onMouseLeave={() => setFocused(false)}
      style={{ cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: '7px' }}
    >
      <div style={{
        position: 'relative', overflow: 'hidden',
        transform: focused ? 'scale(1.04)' : 'scale(1)',
        transition: 'transform 0.18s ease',
        borderRadius: V2.radiusArt,
        boxShadow: focused
          ? `0 8px 28px rgba(0,0,0,0.4), 0 0 0 2px ${V2.brand}, 0 0 18px rgba(139,116,232,0.6)`
          : 'none',
      }}>
        <GameCover romId={game.rom_id} hasCover={game.has_cover}
          onLoaded={(u) => { uriRef.current = u; }} />
        {dl && (
          <div style={{
            position: 'absolute', top: '6px', right: '6px',
            width: '10px', height: '10px', borderRadius: '50%',
            background: V2.success, boxShadow: '0 0 0 2px rgba(0,0,0,0.45)',
          }} />
        )}

        {/* GameActions overlay — gradient scrim, center primary (download/play),
            bottom Details + Delete; revealed on hover/focus. */}
        <div style={{
          position: 'absolute', inset: 0, borderRadius: V2.radiusArt, pointerEvents: 'none',
          background: 'linear-gradient(to top, rgba(0,0,0,0.78) 0%, rgba(0,0,0,0) 55%)',
          opacity: focused ? 1 : 0, transition: 'opacity 0.18s ease',
        }} />
        {/* Center primary (A) — emphasized white round button (RomM Play). */}
        <div
          onClick={(e: any) => { e.stopPropagation(); primary(); }}
          style={{
            position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
            ...roundBtn(44, 'emphasized'), boxShadow: '0 2px 10px rgba(0,0,0,0.55)',
            opacity: focused ? 1 : 0, transition: 'opacity 0.18s ease',
          }}>
          {busy === 'download' || busy === 'launch'
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
            <div onClick={(e: any) => { e.stopPropagation(); doDelete(); }} style={roundBtn(30, 'danger')}>
              {busy === 'delete' ? <FaSync size={12} style={{ animation: 'spin 1s linear infinite' }} /> : <FaTrash size={12} />}
            </div>
          )}
        </div>
      </div>
      <div style={{
        fontSize: '11.5px', color: focused ? V2.fg : V2.fg2, textAlign: 'center',
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        transition: 'color 0.18s', padding: '0 1px',
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
      try { const r = await getImage(path); if (alive) setUri(r?.data_uri || null); }
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

// Collection tile — mosaic cover + kind badge + name/count below, with the
// focus scale + brand glow (RomM CollectionTile).
function CollectionTile({ group, onOpen }: { group: LibGroup; onOpen: (g: LibGroup) => void }) {
  const [focused, setFocused] = useState(false);
  const badge = group.kind === 'smart' ? { label: 'SMART', bg: V2.brandHover }
    : group.kind === 'virtual' ? { label: 'VIRTUAL', bg: V2.brandHover }
    : group.kind === 'favorite' ? { label: '★', bg: '#ff4f6b' }
    : null;
  return (
    <Focusable
      onActivate={() => onOpen(group)} onClick={() => onOpen(group)}
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
        {badge && (
          <span style={{
            position: 'absolute', top: '6px', left: '6px',
            fontSize: '9px', fontWeight: 800, letterSpacing: '0.04em',
            padding: '2px 7px', borderRadius: V2.radiusChip, color: '#fff',
            background: badge.bg, boxShadow: '0 1px 3px rgba(0,0,0,0.5)',
          }}>{badge.label}</span>
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
          const r = await getImage(c);
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
function PlatformTile({ group, onOpen }: { group: LibGroup; onOpen: (g: LibGroup) => void }) {
  const [focused, setFocused] = useState(false);
  return (
    <Focusable
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
function V2NavBar({ active, onTab }: { active: NavId; onTab: (id: NavId) => void }) {
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
      <Focusable flow-children="horizontal" style={{
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
            <Focusable key={id} onActivate={() => onTab(id)} onClick={() => onTab(id)}>
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
          <Focusable key={t.id} onActivate={() => onTab(t.id)} onClick={() => onTab(t.id)}>
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
    <Focusable
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

// Module-level holders pass the selection between Game Browser routes without
// re-fetching (same pattern as _historyGameHolder).
let _libGroupHolder: { mode: string; group: LibGroup } | null = null;
// Sibling groups (same mode) so the game grid can page prev/next with L1/R1.
let _libGroupsHolder: { mode: string; groups: LibGroup[] } | null = null;
let _libGameHolder: LibGame | null = null;

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
const V2_FOCUS_STYLE = `
  .romm-ui [class*="gpfocus"] { outline: none !important; box-shadow: none !important; }
  .romm-ui [class*="gpfocus"]::before, .romm-ui [class*="gpfocus"]::after {
    box-shadow: none !important; background: none !important; border: none !important; content: none !important;
  }
  /* Keep a tasteful on-brand ring on buttons so they still show focus. */
  .romm-ui .romm-btn.gpfocuswithin, .romm-ui .romm-btn.gpfocus {
    box-shadow: 0 0 0 2px ${'rgba(139,116,232,0.9)'} !important;
  }
`;

function v2Page(children: any, bgUri: string | null = null) {
  return (
    <div className="romm-ui" style={{
      fontFamily: V2.font, color: V2.fg, background: V2.bg,
      position: 'relative', overflowY: 'auto', height: 'calc(100vh - 40px)',
      marginTop: '40px',
    }}>
      <style>{V2_FOCUS_STYLE}</style>
      <V2Bg uri={bgUri} />
      <div style={{ position: 'relative', zIndex: 2, padding: '0 0 40px' }}>
        {children}
      </div>
    </div>
  );
}

const NAV_ORDER: NavId[] = ['home', 'platforms', 'collections', 'search'];

// RomM RTextField (filled) search field — rounded 8px box, bg-elevated fill,
// magnify prefix, clear (×) suffix, brand border + halo on focus.
function V2SearchField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [focused, setFocused] = useState(false);
  return (
    <Focusable onActivate={() => inputRef.current?.focus()} style={{ borderRadius: V2.radiusMd }}>
      <style>{`.v2-search-input::placeholder{color:rgba(255,255,255,0.45)}`}</style>
      <div style={{
        display: 'flex', alignItems: 'center', gap: '10px', height: '40px', padding: '0 12px',
        borderRadius: V2.radiusMd,
        background: focused ? V2.surfaceHover : 'rgba(255,255,255,0.045)',
        border: `1px solid ${focused ? V2.brand : V2.border}`,
        boxShadow: focused ? `0 0 0 3px rgba(139,116,232,0.22)` : 'none',
        transition: 'background 0.2s, border-color 0.2s, box-shadow 0.2s',
      }}>
        <FaSearch size={14} color={focused ? V2.brandHover : V2.fgMuted} style={{ flexShrink: 0 }} />
        <input
          ref={inputRef}
          className="v2-search-input"
          value={value}
          placeholder="Search games"
          onChange={(e: ChangeEvent<HTMLInputElement>) => onChange(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          style={{
            flex: '1 1 auto', minWidth: 0, background: 'transparent', border: 'none', outline: 'none',
            color: V2.fg, fontSize: '14px', fontFamily: 'inherit', padding: 0,
          }}
        />
        {value && (
          <div onClick={() => { onChange(''); inputRef.current?.focus(); }}
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
}

// Search tab — debounced text filter over the whole library, results as a
// cover-art grid (the nav 'Search' destination).
function SearchPanel({ onOpen, onBg }: { onOpen: (g: LibGame) => void; onBg: (uri: string | null) => void }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState<LibGame[]>([]);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!q.trim()) { setResults([]); setLoading(false); return; }
    // Enter the loading state synchronously so the debounce window shows
    // "Searching…" rather than briefly flashing the "no match" empty state.
    setLoading(true);
    const t = setTimeout(async () => {
      try { const r = await searchGames(q); setResults(r?.success ? (r.games || []) : []); }
      catch { setResults([]); }
      finally { setLoading(false); }
    }, 250);
    return () => clearTimeout(t);
  }, [q]);
  return (
    <div style={{ padding: '0 16px' }}>
      <div style={{ maxWidth: '520px', margin: '0 auto 16px' }}>
        <V2SearchField value={q} onChange={setQ} />
      </div>
      {loading ? (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px', textAlign: 'center' }}>Searching…</div>
      ) : !q.trim() ? (
        <div style={{ padding: '24px', color: V2.fgMuted, fontSize: '13px', textAlign: 'center' }}>
          Type to search your library.
        </div>
      ) : results.length === 0 ? (
        <div style={{ padding: '24px', color: V2.fgMuted, fontSize: '13px', textAlign: 'center' }}>
          No games match “{q.trim()}”.
        </div>
      ) : (
        <Focusable style={{
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
          <Focusable style={{ display: 'flex', gap: '12px', padding: '24px 16px 28px' }}>
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
  const [recent, setRecent] = useState<LibGame[]>([]);
  const [continuePlaying, setContinuePlaying] = useState<LibGame[]>([]);
  const [platforms, setPlatforms] = useState<LibGroup[]>([]);
  const [collections, setCollections] = useState<LibGroup[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [h, p, c] = await Promise.all([
          getHomeData(), getLibraryGroups('platform'), getLibraryGroups('collection'),
        ]);
        if (!alive) return;
        if (h?.success) { setRecent(h.recent || []); setContinuePlaying(h.continue_playing || []); }
        if (p?.success) setPlatforms(p.groups || []);
        if (c?.success) setCollections(c.groups || []);
      } catch (e) { console.error('home load failed', e); }
      finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; };
  }, []);

  if (loading) {
    return <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px' }}>Loading…</div>;
  }
  return (
    <div>
      {/* Continue playing — per-user last_played from RomM (cross-device). */}
      {continuePlaying.length > 0 && (
        <CardRow icon={<FaPlay size={14} />} title="Continue playing" count={continuePlaying.length}>
          {continuePlaying.map((g) => (
            <div key={g.rom_id} style={{ width: '132px', flexShrink: 0 }}>
              <GameTile game={g} onOpen={onOpen} onActiveCover={onBg} />
            </div>
          ))}
        </CardRow>
      )}

      {/* Recently added */}
      {recent.length > 0 && (
        <CardRow icon={<FaRegClock size={16} />} title="Recently added" count={recent.length}>
          {recent.map((g) => (
            <div key={g.rom_id} style={{ width: '132px', flexShrink: 0 }}>
              <GameTile game={g} onOpen={onOpen} onActiveCover={onBg} />
            </div>
          ))}
        </CardRow>
      )}

      {/* Platforms */}
      {platforms.length > 0 && (
        <CardRow icon={<FaGamepad size={16} />} title="Platforms" count={platforms.length}>
          {platforms.map((g) => (
            <div key={g.key} style={{ width: '150px', flexShrink: 0 }}>
              <PlatformTile group={g} onOpen={(grp) => onOpenGroup('platform', grp, platforms)} />
            </div>
          ))}
        </CardRow>
      )}

      {/* Collections */}
      {collections.length > 0 && (
        <CardRow icon={<FaLayerGroup size={15} />} title="Collections" count={collections.length}>
          {collections.map((g) => (
            <div key={g.key} style={{ width: '132px', flexShrink: 0 }}>
              <CollectionTile group={g} onOpen={(grp) => onOpenGroup('collection', grp, collections)} />
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
  const [active, setActive] = useState<NavId>('home');
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
    Navigation.Navigate(`/romm-sync-game/${g.rom_id}`);
  };

  const onTab = (id: NavId) => setActive(id);

  // L1 / R1 page through the nav tabs (BackgroundArt cleared on home/search).
  const cycle = (dir: -1 | 1) => {
    const i = NAV_ORDER.indexOf(active);
    setActive(NAV_ORDER[(i + dir + NAV_ORDER.length) % NAV_ORDER.length]);
  };
  const onButtonDown = (evt: any) => {
    const b = evt?.detail?.button;
    if (b === GamepadButton.BUMPER_LEFT) cycle(-1);
    else if (b === GamepadButton.BUMPER_RIGHT) cycle(1);
  };

  return v2Page(
    <Focusable onButtonDown={onButtonDown}>
      <V2NavBar active={active} onTab={onTab} />

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
        <Focusable
          style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
            gap: '14px', padding: '0 16px',
          }}
        >
          {groups.map((g) => <PlatformTile key={g.key} group={g} onOpen={openGroup} />)}
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
                <Focusable
                  style={{
                    display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))',
                    gap: '18px 16px', padding: '0 16px 8px',
                  }}
                >
                  {items.map((g) => <CollectionTile key={g.key} group={g} onOpen={openGroup} />)}
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
  const [games, setGames] = useState<LibGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [bgUri, setBgUri] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      if (!group) { setLoading(false); return; }
      setLoading(true); setBgUri(null);
      try {
        const res = await getLibraryGames(mode, group.key);
        if (alive) setGames(res?.success ? (res.games || []) : []);
      } catch (e) {
        console.error('get_library_games failed', e);
        if (alive) setGames([]);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [group?.key]);

  const openGame = (g: LibGame) => {
    _libGameHolder = g;
    Navigation.Navigate(`/romm-sync-game/${g.rom_id}`);
  };

  // L1 / R1 page through sibling groups (same mode) without backing out.
  const siblings = (_libGroupsHolder && _libGroupsHolder.mode === mode) ? _libGroupsHolder.groups : [];
  const cycle = (dir: -1 | 1) => {
    if (!group || siblings.length < 2) return;
    const i = siblings.findIndex((s) => s.key === group.key);
    if (i < 0) return;
    const next = siblings[(i + dir + siblings.length) % siblings.length];
    _libGroupHolder = { mode, group: next };
    setGroup(next);
  };
  const onButtonDown = (evt: any) => {
    const b = evt?.detail?.button;
    if (b === GamepadButton.BUMPER_LEFT) cycle(-1);
    else if (b === GamepadButton.BUMPER_RIGHT) cycle(1);
  };

  const jumpTo = (g: LibGroup) => { _libGroupHolder = { mode, group: g }; setGroup(g); };

  if (!group) {
    return v2Page(<div style={{ padding: '16px', color: V2.fgMuted }}>No group selected.</div>);
  }

  const canPage = siblings.length > 1;
  // Context around the current group: 1 previous + up to 3 next, dimmed and
  // dot-separated beside the big current title (no wrap — clearer than a strip).
  const ci = siblings.findIndex((s) => s.key === group.key);
  const prev = ci > 0 ? siblings[ci - 1] : null;
  const nexts: LibGroup[] = [];
  for (let k = 1; k <= 3 && ci + k < siblings.length; k++) nexts.push(siblings[ci + k]);

  const dot = (key: string) => <span key={key} style={{ color: V2.fgMuted, opacity: 0.6, fontSize: '13px' }}>·</span>;
  const sideName = (g: LibGroup) => (
    <Focusable key={g.key} onActivate={() => jumpTo(g)} onClick={() => jumpTo(g)}
      style={{
        cursor: 'pointer', color: V2.fgMuted, fontSize: '13px', maxWidth: '120px',
        flexShrink: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }}>
      {g.label}
    </Focusable>
  );

  return v2Page(
    <Focusable onButtonDown={onButtonDown}>
      <div style={{ padding: '16px 16px 12px' }}>
        <div style={{ fontSize: '12px', color: V2.fgMuted, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
          {mode === 'collection' ? 'Collection' : 'Platform'}
        </div>
        <Focusable flow-children="horizontal" style={{ display: 'flex', alignItems: 'baseline', gap: '8px', marginTop: '2px' }}>
          {canPage && <Bumper label="L1" />}
          {prev && <span style={{ opacity: 0.7, color: V2.fgMuted }}>‹</span>}
          {prev && sideName(prev)}
          {prev && dot('d-prev')}
          <div style={{ fontSize: '24px', fontWeight: 800, letterSpacing: '-0.01em', flexShrink: 0 }}>{group.label}</div>
          {nexts.map((g, i) => [dot(`d-${i}`), sideName(g)])}
          {nexts.length > 0 && <span style={{ opacity: 0.7, color: V2.fgMuted }}>›</span>}
          {ci + nexts.length < siblings.length - 1 && <span style={{ color: V2.fgMuted }}>…</span>}
          {canPage && <Bumper label="R1" />}
        </Focusable>
        {!loading && <div style={{ fontSize: '12px', color: V2.fgMuted, marginTop: '4px' }}>{games.length} {games.length === 1 ? 'game' : 'games'}</div>}
      </div>

      {loading ? (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px' }}>Loading games…</div>
      ) : games.length === 0 ? (
        <div style={{ padding: '16px', color: V2.fgMuted, fontSize: '13px' }}>No games in this group.</div>
      ) : (
        <Focusable
          style={{
            display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))',
            gap: '18px 16px', padding: '6px 16px',
          }}
        >
          {games.map((g) => (
            <GameTile key={g.rom_id} game={g} onOpen={openGame} onActiveCover={setBgUri} />
          ))}
        </Focusable>
      )}
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
      try { const r = await getImage(path); if (alive) setUri(r?.data_uri || null); }
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
      <Focusable onButtonDown={onButtonDown}
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
    <Focusable
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
    <Focusable style={{
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
      <Focusable
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
        /* Filtering collapses/expands rows in place so both entry and exit animate. */
        .ach-slot { overflow: hidden; transition: max-height 280ms ${EASE}, opacity 220ms ease, margin-bottom 280ms ${EASE}; }
        .ach-row { transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ${EASE}, box-shadow 0.15s ease; }
        .ach-row:hover, .ach-row:focus-within {
          background: ${V2.surfaceHover}; border-color: ${V2.borderStrong};
          transform: translateY(-1px); box-shadow: 0 6px 18px rgba(0,0,0,0.35);
        }
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

      <Focusable className="ach-fade" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '6px', '--ach-i': 1 } as any}>
        {(['all', 'progression', 'missable', 'win_condition'] as TypeFilter[]).map((f) =>
          filterBtn(f, typeLabel(f), typeFilter === f, () => setTypeFilter(f)))}
        <span style={{ width: '1px', height: '16px', background: V2.surfaceHover, margin: '0 4px' }} />
        {filterBtn('earned', '✓ Earned', statusFilter === 'earned', () => toggleStatus('earned'), 'earned')}
        {filterBtn('locked', '⊘ Locked', statusFilter === 'locked', () => toggleStatus('locked'), 'locked')}
      </Focusable>

      <Focusable style={{ display: 'flex', flexDirection: 'column' }}>
        {(() => { let vi = 0; return achievements.map((a, i) => {
          const src = a.earned ? a.badge_url : a.badge_url_lock;
          const vis = isVisible(a);
          const delay = vis ? (vi++) * 22 : 0; // stagger the expand of currently-visible rows
          return (
            <div key={a.ra_id ?? i} className="ach-slot" style={{
              maxHeight: vis ? '160px' : '0px',
              opacity: vis ? 1 : 0,
              marginBottom: vis ? '6px' : '0px',
              pointerEvents: vis ? 'auto' : 'none',
              transitionDelay: `${delay}ms`,
            }}>
              <div className="ach-row ach-fade" style={{
                display: 'grid', gridTemplateColumns: '52px 1fr auto', gap: '14px', alignItems: 'center',
                padding: '10px 14px', background: V2.surface, border: `1px solid ${V2.border}`,
                borderRadius: V2.radiusMd, opacity: a.earned ? 1 : 0.55,
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
              </div>
            </div>
          );
        }); })()}
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

  useEffect(() => {
    if (shotUri) return;
    getSaveScreenshot(romId, entry.id, entry.save_type)
      .then((r: any) => setShot(r?.data_uri || null))
      .catch(() => setShot(null))
      .finally(() => setLoadingShot(false));
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
    <Focusable
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
      `}</style>
      <Focusable flow-children="vertical" style={{
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

        <Focusable flow-children="horizontal" style={{ display: 'flex', gap: '8px', flexWrap: 'nowrap', justifyContent: 'flex-end', alignItems: 'center' }}>
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
      <Focusable
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
        .sd-row { transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ${EASE}; }
        .sd-row:hover, .sd-row:focus-within { background: ${V2.surfaceHover}; border-color: ${V2.borderStrong}; transform: translateY(-1px); }
        .sd-tile { transition: transform 0.15s ${EASE}, box-shadow 0.15s ease, border-color 0.15s ease; }
        .sd-tile:hover, .sd-tile:focus-within { transform: translateY(-2px); box-shadow: 0 8px 22px rgba(0,0,0,0.4); border-color: ${V2.borderStrong}; }
        @keyframes sdShimmer { 0% { background-position: -150% 0; } 100% { background-position: 150% 0; } }
        .sd-shimmer { background-image: linear-gradient(100deg, transparent 20%, rgba(255,255,255,0.22) 50%, transparent 80%) !important; background-size: 200% 100% !important; background-repeat: no-repeat; animation: sdShimmer 1.1s linear infinite; }
      `}</style>

      <Focusable flow-children="horizontal" style={{ display: 'flex', gap: '8px' }}>
        {pill('saves', 'Saves', saves.length)}
        {pill('states', 'States', states.length)}
      </Focusable>

      {sub === 'saves' ? (
        saves.length === 0 ? (
          <div style={{ color: V2.fgMuted, fontSize: '12px', padding: '12px 0' }}>No saves for this game.</div>
        ) : (
          <Focusable style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {groupBySlot(saves).map((g) => (
              <div key={`sg-${g.slot}`} style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {slotHeader(g.slot, g.items.length)}
                {g.items.map((e, idx) => {
                  const isCur = idx === 0;
                  const sub2 = [e.device || '', fmtHistSize(e.size_bytes)].filter(Boolean).join(' · ');
                  return (
                    <div key={`save-${e.id}`} className="sd-fade" style={{ '--sd-i': Math.min(idx, 14) } as any}>
                      <Focusable
                        className="sd-row"
                        onActivate={() => openRestore(e)}
                        onClick={() => openRestore(e)}
                        style={{
                          display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', textAlign: 'left',
                          padding: '10px 13px', background: V2.surface, cursor: 'pointer',
                          border: `1px solid ${V2.border}`, borderRadius: V2.radiusMd,
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
                <Focusable style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '12px' }}>
                  {g.items.map((e, idx) => {
                    const isCur = idx === 0;
                    const shot = shots[e.id];
                    const loadingShot = shot === undefined || shot === 'loading';
                    return (
                      <Focusable
                        key={`state-${e.id}`}
                        className="sd-tile sd-fade"
                        onActivate={() => openRestore(e)}
                        onClick={() => openRestore(e)}
                        style={{
                          display: 'flex', flexDirection: 'column', cursor: 'pointer', overflow: 'hidden',
                          background: V2.surface, borderRadius: V2.radiusLg,
                          border: `1px solid ${V2.border}`,
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

function GameDetailPage() {
  const game = _libGameHolder;
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<null | 'download' | 'delete'>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [isDownloaded, setIsDownloaded] = useState<boolean>(!!game?.is_downloaded);
  const [bgUri, setBgUri] = useState<string | null>(null);
  const [tab, setTab] = useState('overview');

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

  const doDownload = async () => {
    if (!game) return;
    setBusy('download');
    try {
      const res = await downloadGame(game.rom_id);
      if (res?.success) {
        toaster.toast({ title: 'Downloaded', body: detail?.name || game.name });
        setIsDownloaded(true);
      } else {
        toaster.toast({ title: 'Download failed', body: res?.message || 'Unknown error' });
      }
    } catch (e) {
      toaster.toast({ title: 'Download failed', body: String(e) });
    } finally {
      setBusy(null);
    }
  };

  const doDelete = async () => {
    if (!game) return;
    setBusy('delete');
    try {
      const res = await deleteGame(game.rom_id);
      if (res?.success) {
        toaster.toast({ title: 'Deleted', body: detail?.name || game.name });
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
  const year = fmtReleaseYear(detail?.release_date);
  // RomM detail header: title + a single "·"-separated meta line
  // (platform · year · size · state), not a row of chips.
  const meta: { text: string; color?: string }[] = [];
  if (platform) meta.push({ text: platform });
  if (year) meta.push({ text: year });
  if (detail?.fs_size_bytes) meta.push({ text: fmtBytes(detail.fs_size_bytes) });
  meta.push(isDownloaded
    ? { text: 'Downloaded', color: V2.success }
    : { text: 'Not downloaded', color: V2.fgMuted });

  // Overview "InfoGrid" sections — label + chip items (RomM InfoGrid).
  const infoGrid: { label: string; items: string[] }[] = [];
  if (detail?.genres?.length) infoGrid.push({ label: 'Genres', items: detail.genres });
  if (detail?.franchises?.length) infoGrid.push({ label: 'Franchise', items: detail.franchises });
  if (detail?.companies?.length) infoGrid.push({ label: 'Developer', items: detail.companies });

  // Tab strip — Files only when the server reported files (RomM hides empty tabs).
  const tabList: { id: string; label: string }[] = [{ id: 'overview', label: 'Overview' }];
  if (detail?.files?.length) tabList.push({ id: 'files', label: 'Files' });
  if (detail?.screenshots?.length) tabList.push({ id: 'screenshots', label: 'Screenshots' });
  tabList.push({ id: 'save-data', label: 'Save Data' });
  if (detail?.achievements?.length) tabList.push({ id: 'achievements', label: 'Achievements' });

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
  };

  return v2Page(
    <Focusable onButtonDown={onButtonDown} style={{ padding: '20px 16px' }}>
      <Focusable flow-children="horizontal" style={{ display: 'flex', gap: '22px', alignItems: 'flex-start' }}>
        {/* Cover */}
        <div style={{ flex: '0 0 220px', maxWidth: '220px' }}>
          <div style={{ boxShadow: V2.elev2, borderRadius: V2.radiusArt }}>
            <GameCover romId={game.rom_id} hasCover={game.has_cover || !!detail?.has_cover} large
              onLoaded={setBgUri} />
          </div>
        </div>

        {/* Info + actions */}
        <Focusable flow-children="vertical" style={{ flex: '1 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column', gap: '14px' }}>
          <div>
            <div style={{ fontSize: '30px', fontWeight: 800, lineHeight: '1.15', letterSpacing: '-0.01em' }}>{name}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', marginTop: '8px', fontSize: '13px', color: V2.fg2 }}>
              {meta.map((m, i) => (
                <span key={i} style={{ display: 'inline-flex', alignItems: 'center' }}>
                  {i > 0 && <span style={{ color: V2.fgMuted, margin: '0 8px' }}>·</span>}
                  <span style={{ color: m.color || V2.fg2 }}>{m.text}</span>
                </span>
              ))}
            </div>
          </div>

          {/* Actions */}
          <Focusable flow-children="horizontal" style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            {!isDownloaded ? (
              <V2Button variant="primary" disabled={!!busy} onClick={doDownload}>
                <FaSync size={13} style={{ animation: busy === 'download' ? 'spin 1s linear infinite' : 'none' }} />
                <span>{busy === 'download' ? 'Downloading…' : 'Download'}</span>
              </V2Button>
            ) : !confirmDelete ? (
              <V2Button variant="tonal" color={V2.danger} onClick={() => setConfirmDelete(true)}>
                <FaTrash size={13} /><span>Delete download</span>
              </V2Button>
            ) : (
              <>
                <V2Button variant="danger" disabled={!!busy} onClick={doDelete}>
                  <FaTrash size={13} /><span>{busy === 'delete' ? 'Deleting…' : 'Confirm delete'}</span>
                </V2Button>
                <V2Button variant="text" onClick={() => setConfirmDelete(false)}>Cancel</V2Button>
              </>
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
              <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                {detail?.summary && (
                  <div style={{ fontSize: '13px', color: V2.fg2, lineHeight: '1.6' }}>{detail.summary}</div>
                )}
                {infoGrid.length > 0 && (
                  <div style={{
                    display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '16px 24px',
                  }}>
                    {infoGrid.map((s) => (
                      <div key={s.label} style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', color: V2.fgMuted }}>
                          {s.label}
                        </div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                          {s.items.map((it, i) => (
                            <span key={i} style={{
                              fontSize: '12px', padding: '3px 10px', borderRadius: V2.radiusChip,
                              background: V2.surface, color: V2.fg2, border: `1px solid ${V2.border}`,
                            }}>{it}</span>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {!detail?.summary && infoGrid.length === 0 && (
                  <div style={{ color: V2.fgMuted, fontSize: '12px' }}>No metadata available.</div>
                )}
              </div>
            ) : tab === 'files' ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
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
            ) : tab === 'screenshots' ? (
              <ScreenshotGrid paths={detail?.screenshots || []} />
            ) : tab === 'save-data' ? (
              <SaveDataTab romId={game.rom_id} />
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

function SettingsPage() {
  const [loggingEnabled, setLoggingEnabled] = useState<boolean>(true);
  const [loading, setLoading] = useState<boolean>(true);
  const [confirmReset, setConfirmReset] = useState<boolean>(false);
  const [resetting, setResetting] = useState<boolean>(false);

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

  const handleReset = async () => {
    setResetting(true);
    try {
      const result = await resetAllSettings();
      if (result?.success) {
        toaster.toast({ title: 'Reset complete', body: `${result.deleted_roms ?? 0} ROM file(s) deleted. Sync state cleared.` });
        // Navigate to the config/welcome page so the user goes through onboarding
        Navigation.Navigate("/romm-sync-config");
        Navigation.CloseSideMenus();
      } else {
        toaster.toast({ title: 'Reset failed', body: result?.error ?? 'Unknown error' });
      }
    } catch (error) {
      toaster.toast({ title: 'Reset failed', body: String(error) });
    } finally {
      setResetting(false);
      setConfirmReset(false);
    }
  };

  return (
    <div style={{ overflowY: 'auto', height: 'calc(100vh - 40px)', marginTop: "40px", paddingBottom: '40px', color: "white" }}>
      <div className={staticClasses.Title} style={{ marginBottom: "20px" }}>RomM Sync Settings</div>
      <PanelSection title="Connection">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              Navigation.Navigate("/romm-sync-config");
              Navigation.CloseSideMenus();
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <FaCog size={14} />
              <span>Configure RomM Connection</span>
            </div>
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Debug Settings">
        <PanelSectionRow>
          <ToggleField
            label="Enable Debug Logging"
            description="Write debug logs to ~/.config/romm-retroarch-sync/decky_debug.log"
            checked={loggingEnabled}
            onChange={handleLoggingToggle}
            disabled={loading}
          />
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="About">
        <PanelSectionRow>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '6px', padding: '4px 0 8px' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px' }}>
              <span style={{ fontWeight: 'bold', fontSize: '15px' }}>RomM RetroArch Sync</span>
              <span style={{ color: '#9ca3af', fontSize: '11px' }}>v1.5.0</span>
            </div>
            <div style={{ color: '#9ca3af', fontSize: '11px' }}>by Covin</div>
            <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
              {[
                { icon: <FaGithub size={12} />, label: 'GitHub', url: 'https://github.com/Covin90/romm-retroarch-sync' },
                { icon: <FaBug size={12} />, label: 'Report Issue', url: 'https://github.com/Covin90/romm-retroarch-sync/issues' },
              ].map(({ icon, label, url }) => (
                <div
                  key={label}
                  onClick={() => Navigation.NavigateToExternalWeb(url)}
                  style={{ display: 'flex', alignItems: 'center', gap: '5px', padding: '4px 10px', borderRadius: '4px', background: 'rgba(255,255,255,0.08)', cursor: 'pointer', fontSize: '12px' }}
                >
                  {icon}
                  <span>{label}</span>
                </div>
              ))}
            </div>
          </div>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Danger Zone">
        {!confirmReset ? (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => setConfirmReset(true)}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#f87171' }}>
                <FaTrash size={14} />
                <span>Reset to New User</span>
              </div>
            </ButtonItem>
            <div style={{ fontSize: '11px', color: '#9ca3af', marginTop: '4px' }}>
              Deletes all synced ROMs and clears sync state. Credentials are kept.
            </div>
          </PanelSectionRow>
        ) : (
          <PanelSectionRow>
            <div style={{ fontSize: '13px', color: '#fbbf24', marginBottom: '8px' }}>
              This will delete all downloaded ROM files and clear collection sync state. Are you sure?
            </div>
            <ButtonItem
              layout="below"
              onClick={handleReset}
              disabled={resetting}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#f87171' }}>
                <FaTrash size={14} />
                <span>{resetting ? 'Resetting...' : 'Yes, reset everything'}</span>
              </div>
            </ButtonItem>
            <ButtonItem
              layout="below"
              onClick={() => setConfirmReset(false)}
              disabled={resetting}
            >
              Cancel
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>
    </div>
  );
}

function Content() {
  const [status, setStatus] = useState<any>({ status: 'loading', message: 'Loading...' });
  const [loading, setLoading] = useState(false);
  const [togglingCollection, setTogglingCollection] = useState<string | null>(null);
  const [steamSyncingCollection, setSteamSyncingCollection] = useState<string | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const configuredRef = useRef<boolean | null>(null);
  const intervalRef = useRef<any>(null);
  const optimisticOverrides = useRef<Map<string, { auto_sync: boolean, sync_state: string, downloaded?: number, total?: number }>>(new Map());
  const [biosExpanded, setBiosExpanded] = useState(false);

  const getStatusColor = () => {
    switch (status.status) {
      case 'connected': return '#4ade80'; // green
      case 'running': return '#fbbf24'; // yellow
      case 'stopped': return '#f87171'; // red
      case 'error': return '#f87171'; // red
      default: return '#9ca3af'; // gray
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
      // If not yet configured, re-check so the panel transitions automatically
      // after the user saves from ConfigPage — without calling getConfig() every cycle.
      if (configuredRef.current === false) {
        const isConfigured = await checkConfigured();
        if (!isConfigured) return;
      }

      const result = await getServiceStatus();

      // Check if backend data matches any overrides - if so, clear them
      if (optimisticOverrides.current.size > 0) {
        result.collections.forEach((col: any) => {
          const override = optimisticOverrides.current.get(col.name);
          if (override && override.auto_sync === col.auto_sync) {
            const backendHasProgress = (col.downloaded !== undefined && col.total !== undefined && col.total > 0);
            const shouldClear = backendHasProgress && (
              override.sync_state === col.sync_state ||
              col.sync_state === 'synced'
            );
            if (shouldClear) {
              console.log(`[REFRESH] Clearing override for ${col.name}`);
              optimisticOverrides.current.delete(col.name);
            }
          }
        });
      }

      // Apply remaining optimistic overrides
      if (optimisticOverrides.current.size > 0) {
        const modifiedResult = {
          ...result,
          collections: result.collections.map((col: any) => {
            const override = optimisticOverrides.current.get(col.name);
            if (override) {
              return {
                ...col,
                auto_sync: override.auto_sync,
                sync_state: override.sync_state,
                ...(override.downloaded !== undefined ? { downloaded: override.downloaded } : {}),
                ...(override.total !== undefined ? { total: override.total } : {})
              };
            }
            return col;
          })
        };
        setStatus(modifiedResult);
      } else {
        setStatus(result);
      }
    } catch (error) {
      setStatus({ status: 'error', message: '❌ Plugin error' });
    }
  };

  const stopPolling = () => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  };

  const startPolling = () => {
    stopPolling();
    intervalRef.current = setInterval(refreshStatus, 2000); // Poll every 2 seconds for more responsive UI
  };

  useEffect(() => {
    checkConfigured().then(refreshStatus);
    startPolling();
    return () => stopPolling();
  }, []);

  const handleReconnect = async () => {
    setLoading(true);
    setTimeout(refreshStatus, 1500);
    setLoading(false);
  };

  const handleToggleCollection = async (collectionName: string, enabled: boolean) => {
    console.log(`[TOGGLE] Starting toggle for ${collectionName}, enabled=${enabled}`);

    // Get current collection state to determine total
    const currentCollection = status?.collections.find((c: any) => c.name === collectionName);
    const totalRoms = currentCollection?.total;
    const hasValidTotal = totalRoms !== undefined && totalRoms > 0;

    // FIRST: Set override BEFORE anything else to protect against concurrent polling
    // Only include downloaded: 0, total: X if we have a valid total (> 0)
    // Otherwise, let backend fetch the real total from server
    optimisticOverrides.current.set(collectionName, {
      auto_sync: enabled,
      sync_state: enabled ? 'syncing' : 'not_synced',
      ...(enabled && hasValidTotal ? { downloaded: 0, total: totalRoms } : {})
    });
    console.log(`[TOGGLE] Set override for ${collectionName} with downloaded=0, total=${hasValidTotal ? totalRoms : 'none'}, map size:`, optimisticOverrides.current.size);

    setTogglingCollection(collectionName);

    // Update UI immediately - change auto_sync and set initial sync_state
    setStatus((prevStatus: any) => {
      const updatedCollections = prevStatus.collections.map((col: any) => {
        if (col.name === collectionName) {
          // When enabling, show as syncing (with 0/total if we have valid total)
          // When disabling, show as not_synced
          if (enabled) {
            if (hasValidTotal) {
              return { ...col, auto_sync: true, sync_state: 'syncing', downloaded: 0, total: totalRoms };
            } else {
              // No valid total yet - just set syncing, let backend populate counts
              return { ...col, auto_sync: true, sync_state: 'syncing' };
            }
          } else {
            return { ...col, auto_sync: false, sync_state: 'not_synced' };
          }
        }
        return col;
      });
      return {
        ...prevStatus,
        collections: updatedCollections,
        actively_syncing_count: updatedCollections.filter((c: any) => c.auto_sync).length
      };
    });

    try {
      console.log(`[TOGGLE] Calling backend toggleCollectionSync...`);
      const result = await toggleCollectionSync(collectionName, enabled);
      console.log(`[TOGGLE] Backend returned:`, result);
      if (!enabled) {
        // Fire ROM deletion in background — don't block the UI refresh on it
        deleteCollectionRoms(collectionName).catch((e: any) =>
          console.error('[TOGGLE] ROM deletion failed:', e)
        );
      }
      console.log(`[TOGGLE] Forcing immediate refresh after backend call`);
      await refreshStatus();
    } catch (error) {
      console.error('[TOGGLE] Failed to toggle collection sync:', error);
      optimisticOverrides.current.delete(collectionName);
      refreshStatus();
    } finally {
      setTogglingCollection(null);
    }
  };

  const handleToggleSteamSync = async (collectionName: string, enabled: boolean) => {
    setSteamSyncingCollection(collectionName);
    try {
      const result = await toggleCollectionSteamSync(collectionName, enabled);
      if (result?.success) {
        toaster.toast({
          title: enabled ? "Added to Steam" : "Removed from Steam",
          body: result.message + (enabled ? "\nRestart Steam to see changes" : ""),
          duration: 4000,
        });
      } else {
        toaster.toast({
          title: "Steam Sync Error",
          body: result?.message || "Unknown error",
          duration: 4000,
        });
      }
      await refreshStatus();
    } catch (error) {
      console.error('Failed to toggle Steam sync:', error);
    } finally {
      setSteamSyncingCollection(null);
    }
  };

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
            Connect your SteamOS device to your RomM server to automatically sync ROMs and save files.
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              Navigation.Navigate("/romm-sync-config");
              Navigation.CloseSideMenus();
            }}
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
      <div style={{ paddingLeft: 0, paddingTop: '8px', paddingBottom: '8px', fontSize: '0.9em', color: '#b0b0b0' }}>
        Status:
      </div>
      <div style={{ paddingLeft: 0, paddingTop: '8px', paddingBottom: '8px', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.85em' }}>
        <div style={{
          width: '8px',
          height: '8px',
          borderRadius: '50%',
          backgroundColor: getStatusColor()
        }} />
        <span>{status.message.replace(', ', ' - ')}</span>
      </div>

      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => { Navigation.Navigate("/romm-sync-library"); Navigation.CloseSideMenus(); }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <FaGamepad size={14} />
            <span>Browse Library</span>
          </div>
        </ButtonItem>
      </PanelSectionRow>

      {status.status === 'running' && status.details?.last_update && (
        <>
          <PanelSectionRow>
            <div style={{ fontSize: '0.82em', color: '#fbbf24', lineHeight: '1.4' }}>
              Not connected to RomM. Check your connection settings or retry.
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={handleReconnect} disabled={loading}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <FaSync size={12} />
                <span>Retry Connection</span>
              </div>
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => { Navigation.Navigate("/romm-sync-config"); Navigation.CloseSideMenus(); }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <FaCog size={12} />
                <span>Connection Settings</span>
              </div>
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}

      {/* BIOS Status Section - always show when there are platforms with BIOS requirements */}
      {status.bios_status && status.bios_status.total_platforms > 0 && (
        status.bios_status.downloading_count > 0 ||
        status.bios_status.failed_count > 0 ||
        status.bios_status.platforms_ready < status.bios_status.total_platforms
      ) && (
          <>
            {/* BIOS Container - includes both summary and expanded list */}
            <div
              style={{
                margin: '4px 0',
                padding: '8px 12px',
                background: status.bios_status.platforms_ready === status.bios_status.total_platforms
                  ? 'rgba(74, 222, 128, 0.12)'
                  : 'rgba(251, 191, 36, 0.12)',
                border: status.bios_status.platforms_ready === status.bios_status.total_platforms
                  ? '1px solid rgba(74, 222, 128, 0.4)'
                  : '1px solid rgba(251, 191, 36, 0.4)',
                borderRadius: '6px',
                fontSize: '13px',
                color: status.bios_status.platforms_ready === status.bios_status.total_platforms
                  ? '#4ade80'
                  : '#fbbf24',
                lineHeight: '1.5',
              }}
            >
              {/* Summary header - clickable */}
              <div
                onClick={() => setBiosExpanded(!biosExpanded)}
                style={{
                  cursor: 'pointer',
                }}
              >
                {status.bios_status.platforms_ready === status.bios_status.total_platforms ? '✅' : '⚠️'} {status.bios_status.platforms_ready}/{status.bios_status.total_platforms} platforms ready
                <div style={{ fontSize: '11px', marginTop: '4px', opacity: 0.8 }}>
                  {biosExpanded ? '▼ Click to collapse' : '▶ Click to expand'}
                </div>
              </div>

              {/* Expanded platform list - inside container */}
              {biosExpanded && status.bios_status.platforms && (
                <div style={{ marginTop: '12px', paddingTop: '8px', borderTop: '1px solid rgba(255, 255, 255, 0.1)' }}>
                  {Object.entries(status.bios_status.platforms).map(([slug, platform]: [string, any]) => {
                    const isDownloading = status.bios_status.downloading?.includes(slug);
                    const hasFailed = status.bios_status.failures?.[slug];
                    let icon = '✅';
                    let color = '#4ade80';
                    let statusText = 'Ready';

                    if (isDownloading) {
                      icon = '📥';
                      color = '#60a5fa';
                      statusText = 'Downloading...';
                    } else if (hasFailed && hasFailed !== 'unavailable_on_server') {
                      icon = '❌';
                      color = '#f87171';
                      statusText = 'Failed';
                    } else if (hasFailed === 'unavailable_on_server' || platform.present === 0) {
                      // Zero BIOS files - needs attention (lenient logic)
                      icon = '⚠️';
                      color = '#fbbf24';
                      statusText = 'Missing';
                    } else if (platform.present > 0) {
                      // Has at least one BIOS - functional (lenient logic)
                      icon = '✅';
                      color = '#4ade80';
                      // Show detail: "Ready (2/3)" if partial, "Ready" if complete
                      if (platform.missing > 0) {
                        statusText = `Ready (${platform.present}/${platform.total_required})`;
                      } else {
                        statusText = `Ready (${platform.present})`;
                      }
                    }

                    return (
                      <div
                        key={slug}
                        style={{
                          padding: '6px 10px',
                          margin: '4px 0',
                          background: 'rgba(0, 0, 0, 0.2)',
                          borderRadius: '4px',
                          fontSize: '12px',
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <span>{icon}</span>
                          <span>{platform.name}</span>
                        </div>
                        <span style={{ color, fontSize: '11px' }}>{statusText}</span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

          </>
        )}

      {status.collections && status.collections.length > 0 && (
        <>
          <div style={{ paddingLeft: 0, paddingTop: '8px', paddingBottom: '8px', fontSize: '0.9em', color: '#b0b0b0' }}>
            Collections:
          </div>
          {status.collections.map((collection: any, index: number) => {
            // Determine dot color based on sync state
            const getDotColor = () => {
              if (!collection.auto_sync) return '#6b7280'; // gray - not syncing
              switch (collection.sync_state) {
                case 'synced': return '#4ade80';    // green - fully synced
                case 'syncing': return '#fb923c';   // orange - currently syncing
                case 'not_synced': return '#f87171'; // red - not synced
                default: return '#6b7280';           // gray - unknown
              }
            };

            const isSyncing = collection.auto_sync && collection.sync_state === 'syncing';
            const hasCount = collection.downloaded !== undefined && collection.total !== undefined && collection.total > 0;
            // Use downloaded_pct (0–100) when available — it's computed on the backend with full
            // float precision and doesn't lose significant digits for large files in large collections.
            // Fall back to the ratio for the rare case where it's missing.
            const pct = isSyncing && collection.downloaded_pct !== undefined
              ? Math.round(collection.downloaded_pct)
              : (hasCount ? Math.round((collection.downloaded / collection.total) * 100) : 0);

            return (
              <div key={index}>
                <PanelSectionRow>
                  <ToggleField
                    label={
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                          <div style={{
                            width: '6px',
                            height: '6px',
                            borderRadius: '50%',
                            backgroundColor: getDotColor()
                          }} />
                          <span>{collection.name}</span>
                        </div>
                        {status.steam_available && collection.auto_sync && hasCount && (
                          <div
                            onClick={(e: React.MouseEvent<HTMLDivElement>) => { e.stopPropagation(); if (steamSyncingCollection !== collection.name && !collection.is_syncing_steam) handleToggleSteamSync(collection.name, !collection.steam_sync); }}
                            title={steamSyncingCollection === collection.name || collection.is_syncing_steam
                              ? 'Syncing Steam shortcuts...'
                              : (collection.steam_sync
                                ? (collection.steam_shortcut_count > 0 ? `${collection.steam_shortcut_count} Steam shortcuts` : 'Remove from Steam')
                                : 'Add to Steam')}
                            style={{ display: 'flex', alignItems: 'center', padding: '2px 4px', borderRadius: '4px', cursor: steamSyncingCollection === collection.name || collection.is_syncing_steam ? 'default' : 'pointer' }}
                          >
                            {steamSyncingCollection === collection.name || collection.is_syncing_steam
                              ? <FaSync size={14} color="#66c0f4" style={{ animation: 'spin 1s linear infinite' }} />
                              : <FaSteam size={16} color={collection.steam_sync ? '#66c0f4' : '#6b7280'} />
                            }
                          </div>
                        )}
                      </div>
                    }
                    description={(() => {
                      const countText = collection.auto_sync
                        ? (hasCount ? `${Math.floor(collection.downloaded)} / ${collection.total} ROMs` : "Fetching...")
                        : (hasCount ? `${Math.floor(collection.downloaded)} / ${collection.total} ROMs locally` : "Fetching...");
                      if (isSyncing && hasCount) {
                        return (
                          <div>
                            <div>{countText}</div>
                            <div style={{ width: '100%', height: '4px', background: 'rgba(255,255,255,0.12)', borderRadius: '2px', overflow: 'hidden', margin: '5px 0' }}>
                              <div style={{ width: `${pct}%`, height: '100%', background: '#fb923c', borderRadius: '2px', transition: 'width 0.4s ease' }} />
                            </div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: '#9ca3af' }}>
                              <span>{pct}%</span>
                              {collection.speed > 0 && <span>{formatSpeed(collection.speed)}</span>}
                            </div>
                          </div>
                        );
                      }
                      return countText;
                    })()}
                    checked={collection.auto_sync}
                    onChange={(value: boolean) => handleToggleCollection(collection.name, value)}
                    disabled={togglingCollection === collection.name}
                  />
                </PanelSectionRow>
              </div>
            );
          })}
        </>
      )}
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

export default definePlugin(() => {
  routerHook.addRoute("/romm-sync-settings", () => <SettingsPage />, { exact: true });
  routerHook.addRoute("/romm-sync-config", () => <ConfigPage />, { exact: true });
  routerHook.addRoute("/romm-sync-library", () => <LibraryGroupsPage />, { exact: true });
  routerHook.addRoute("/romm-sync-library/:key", () => <LibraryGamesPage />, { exact: true });
  routerHook.addRoute("/romm-sync-game/:romId", () => <GameDetailPage />, { exact: true });

  return {
    name: "RomM Sync Monitor",
    titleView: <TitleView />,
    content: <Content />,
    icon: <FaSync />,
    onDismount: () => {
      console.log('[PLUGIN] onDismount - Stopping background monitoring');
      stopBackgroundMonitoring();

      routerHook.removeRoute("/romm-sync-settings");
      routerHook.removeRoute("/romm-sync-config");
      routerHook.removeRoute("/romm-sync-library");
      routerHook.removeRoute("/romm-sync-library/:key");
      routerHook.removeRoute("/romm-sync-game/:romId");
    },
  };
});