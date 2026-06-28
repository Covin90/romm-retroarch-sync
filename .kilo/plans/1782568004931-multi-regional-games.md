# Multi-Regional Games Implementation Plan

## Goal
Add multi-region support to the Decky plugin: single tile per game with a region picker (like the existing disc picker), where selecting a region downloads that variant. If the selected region is also multi-disc, a disc picker follows.

## Data Model (RomM API)

RomM already provides `sibling_roms` on each ROM — these are regional/language variants (USA, Japan, Europe) of the same game. The `RomMClient._group_sibling_roms()` method (sync_core.py:1980) groups them under a main ROM with `_sibling_files` attached. The Decky plugin already receives this data:

- `_available_games` stores `_sibling_files` (main.py:363, 792)
- `_serialize_game` does NOT currently expose sibling data to the frontend
- `get_game_detail` returns `regions`, `languages`, `tags` from the API (main.py:2283-2285)

**Key insight**: Each sibling ROM has its own `rom_id`, `name`, `platform_slug`, `fs_name`, and can be independently downloaded. A sibling may itself be multi-disc.

## Design Decisions

1. **Single tile + region picker**: One tile per game. A-press opens region picker if multi-region.
2. **Region first, disc nested**: If multi-region, show region picker. If selected region is multi-disc, show disc picker after.
3. **Keep both regions downloaded**: When switching regions, keep old files and download new alongside. No deletion.
4. **Region count badge**: Top-left corner (globe icon + count). Disc badge stays bottom-left. Separate corners, no overlap.
5. **Picker sort order**: Downloaded regions first, then alphabetical.
6. **Saves per variant**: Each regional variant has its own save data (already handled by existing 3-tier `find_rom_id_for_save_file` matching). No cross-sharing — this is correct because save formats may differ between regions.
7. **Mixed single/multi-disc regions**: Explicitly handled — each variant's disc structure is independent.

## Implementation

### Backend (main.py)

#### 1. Add `sibling_roms` field to API requests
Currently `get_roms()` in `_connect_to_romm` (line 330) and `refresh_from_romm` (line 694) call `RomMClient.get_roms()` which internally uses `sync_core.get_roms()`. The `_fetch_all_games_chunked` path already requests `sibling_roms` in the fields param (sync_core.py:2150). So sibling data is ALREADY flowing into `_available_games` — we just need to expose it.

**No changes needed** to the API request fields — `sibling_roms` is already requested by `_fetch_all_games_chunked`.

#### 2. Store sibling data in `_available_games`
Currently `_sibling_files` is stored (main.py:363) but this is the grouped data from `_group_sibling_roms` which contains full ROM dicts of variant files. We need the raw `sibling_roms` list (compact: just `id` + `name`) for the region picker.

In `_connect_to_romm` (line 349-363), add:
```python
'sibling_roms': rom.get('sibling_roms', []),
```
Also in `refresh_from_romm` (line 780-792), same addition.

#### 3. Expose sibling data via `_serialize_game`
Add to the returned dict:
```python
if g.get('sibling_roms'):
    s['sibling_roms'] = [
        {'rom_id': sib.get('id'), 'name': sib.get('name') or sib.get('fs_name_no_ext') or 'Variant'}
        for sib in g['sibling_roms']
    ]
    s['region_count'] = len(g['sibling_roms']) + 1  # +1 for the main ROM itself
```

#### 4. Add `get_sibling_roms` endpoint
New async method to fetch full sibling ROM data for the region picker:
```python
async def get_sibling_roms(self, rom_id: int):
    """Return regional variants for a ROM."""
```
Fetches `/api/roms/{rom_id}` and returns `sibling_roms` array with `id`, `name`, `fs_name`, `regions`.

#### 5. Add `get_local_siblings` endpoint
Returns which siblings are already downloaded locally (checks `_games_index` for each sibling's `rom_id`).

#### 6. Update collection/search inline dicts
In `get_library_games` collection path (line 1806-1817) and `search_games` (line 1873-1884), add sibling data from the raw API response:
```python
if r.get('sibling_roms'):
    entry['sibling_roms'] = [
        {'rom_id': s.get('id'), 'name': s.get('name') or 'Variant'}
        for s in r['sibling_roms']
    ]
    entry['region_count'] = len(r['sibling_roms']) + 1
```

#### 7. Add `get_local_siblings` to `_games_index`
Add a helper method that, given a list of sibling `rom_id`s, checks which are downloaded locally:
```python
def _get_local_sibling_status(self, sibling_rom_ids):
    idx = self._games_index()
    return {rid: bool(idx.get(rid, {}).get('is_downloaded')) for rid in sibling_rom_ids}
```

### Frontend (index.tsx)

#### 8. Extend `LibGame` interface
```typescript
interface LibGame {
  // ... existing fields ...
  sibling_roms?: { rom_id: number; name: string }[];
  region_count?: number;
}
```

#### 9. Add `getSiblingRomData` backend call
```typescript
async function getSiblingRomData(romId: number) {
  return await server!.callPluginMethod<{}, { siblings: any[] }>(
    'get_sibling_roms', { rom_id: romId });
}
```

#### 10. Create `openRegionPicker` function
Modeled on `openDiscPicker` (index.tsx:3882-3939). Shows a context menu with:
- Downloaded regions first, then not-yet-downloaded (alphabetical within each group)
- Current/last-used region highlighted with a check mark
- Each region shows: name + download status + disc count if multi-disc
- Selecting a region: if not downloaded, triggers download; if downloaded, launches (or shows disc picker if multi-disc)
- Keeps both regions on disk — no deletion when switching

```typescript
function openRegionPicker(
  romId: number, gameName: string,
  siblings: { rom_id: number; name: string }[],
  downloadedIds: Set<number>,
  lastUsedId?: number,
  onSelected?: (sibRomId: number) => void,
) {
  // Sort: main ROM always first, then siblings downloaded-first, alpha within groups
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
        </MenuItem>
      ))}
    </Menu>
  );
}
```

#### 11. Add region badge to `GameTile`
Top-left corner (separate from disc badge at bottom-left). When `region_count > 1`:
```tsx
{dl && game.region_count && game.region_count > 1 && (
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
```
No stacking needed — region badge top-left, disc badge bottom-left.

#### 12. Modify tile A-press handler
Currently: A-press on downloaded multi-disc game → quick press launches, long-press opens disc picker.

New behavior:
- A-press on multi-region game → opens region picker (replaces long-press disc picker)
- After region selection → if region is multi-disc, opens disc picker; else launches
- Long-press A on multi-region game → same as A-press (opens region picker)
- Long-press A on multi-disc-only game (single region) → opens disc picker (unchanged)

#### 13. Handle region selection flow
When a region is selected from the picker:
1. If the selected `rom_id` differs from the main ROM, call `downloadGame(selectedRomId)` if not downloaded
2. After download, if the selected region is multi-disc, fetch local discs and show disc picker
3. If single disc, launch directly

This requires the tile to track which region is active. Store in component state + pass to `launchGame`.

#### 14. Update `downloadGame` for sibling ROMs
The backend `download_game` already accepts `rom_id` — it works for any ROM ID, including siblings. No backend change needed for downloading. The existing fallback path (main.py:2322-2327) fetches ROM details when the ROM isn't in the local index.

#### 15. Update `launchGame` for sibling ROMs
`launch_game` needs to accept an optional `rom_id` override (currently it only launches the main ROM). Add a `sibling_rom_id` parameter:
```python
async def launch_game(self, rom_id: int, disc: str = None, sibling_rom_id: int = None):
```
If `sibling_rom_id` is provided, look up that ROM's local path instead.

## Save Data

No additional work needed. The existing 3-tier `find_rom_id_for_save_file()` matching (sync_core.py:6857-7000) already correctly associates saves with the specific regional variant:
- **Tier 1**: Exact filename match against `fs_name_no_ext` + `_sibling_files`
- **Tier 2**: Region-aware matching via `_extract_region_tag()` (16 known regions)
- **Tier 3**: Fuzzy match (strips all parenthetical content)

Each regional variant has its own `rom_id` on the RomM server, and RetroArch creates separate `.srm`/`.state` files per ROM filename. No cross-variant sharing — this is correct because save formats may differ between regions.

## Edge Cases

- **Mixed single/multi-disc regions**: Each variant's disc structure is independent. The region picker entry shows disc count; after selection, `_list_local_discs` resolves the correct files for that variant.
- **Missing siblings from API**: If a ROM has no `sibling_roms`, `_serialize_game` omits the field. Frontend checks `region_count > 1` before showing badge/picker.
- **Duplicate sibling groups**: Already prevented by `_group_sibling_roms()` using `tuple(sorted(related_ids))` group key.
- **Cross-platform slug variants**: Impossible in practice — regional variants are always on the same platform.

## File Changes Summary

| File | Changes |
|------|---------|
| `decky_plugin/main.py` | Store `sibling_roms` in `_available_games`; expose in `_serialize_game`; add `get_sibling_roms` endpoint; add `get_local_siblings` helper; update collection/search inline dicts; add `sibling_rom_id` to `launch_game` |
| `decky_plugin/src/index.tsx` | Extend `LibGame`; add `getSiblingRomData`; create `openRegionPicker`; add region badge; modify tile A-press handler; update `downloadGame`/`launchGame` calls |

## Validation

1. Build: `cd decky_plugin && npm run build` — should have no new TS errors (only pre-existing TS7030)
2. Deploy: copy `dist/index.js` + `dist/index.js.map` + `main.py` to `~/homebrew/plugins/romm-sync-monitor/`
3. Test on Steam Deck:
   - Multi-region game tile shows region count badge (top-left, globe icon)
   - A-press on multi-region tile opens region picker
   - Region picker shows downloaded regions first, then alphabetical
   - Selecting undownloaded region triggers download (old region kept on disk)
   - Selecting downloaded region launches (or shows disc picker if multi-disc)
   - Single-region games behave unchanged
   - Multi-disc-only games (no regions) still show disc picker on long-press
   - Mixed regions (single-disc USA + multi-disc Japan) work correctly
   - Saves are per-variant (switching regions starts fresh, no cross-contamination)
