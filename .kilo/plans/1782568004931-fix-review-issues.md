# Fix 6 Issues from Code Review

## Context

The Decky plugin has 6 issues from a code review: 1 high, 3 medium, 2 low severity. All stem from the multi-disc badge, search field, and minor cleanup changes made in this session.

---

## Fix 1 (High): V2SearchField unreachable via gamepad

**File**: `decky_plugin/src/index.tsx:2213-2239`

The `V2SearchField` was changed from `<Focusable>` to a plain `<div>`. Decky's gamepad navigation only discovers elements wrapped in `Focusable`. Without it, gamepad users can't reach the search field in Gaming Mode.

**Fix**: Wrap the search field container in `<Focusable noFocusRing>` and add `onActivate` to focus the `TextField` inside. This restores gamepad navigability while keeping the Steam keyboard integration via `TextField.focusOnMount`.

```tsx
// Replace the outer <div ref={wrapperRef}> with:
<Focusable noFocusRing ref={wrapperRef} onActivate={() => { /* TextField handles keyboard */ }}
  style={{ borderRadius: V2.radiusMd }}>
  ...
</Focusable>
```

Remove the `useEffect` that forwards the ref (no longer needed since we don't need to call `.focus()` externally — `focusOnMount` and the native TextField handle it).

---

## Fix 2 (Medium): Remove filesystem I/O from `_serialize_game`

**File**: `decky_plugin/main.py:1767-1791`

`_serialize_game` calls `_detect_multi_disc()` (filesystem stat+readdir) for every game on every API call. For large libraries this causes hundreds of stat calls per request.

**Fix**:
1. Remove the `_detect_multi_disc` call from `_serialize_game`. Instead, simply propagate `is_multi_disc` and `disc_count` from the game dict if present.
2. After a successful download in `download_game`'s `_worker` (line ~2370-2372), compute and set `is_multi_disc` + `disc_count` on the game dict using `_detect_multi_disc(dest, True)`. This ensures newly-downloaded games get the flag immediately.

```python
# In _worker, after g['is_downloaded'] = True:
is_md, dc = _detect_multi_disc(str(dest), True)
g['is_multi_disc'] = is_md
g['disc_count'] = dc
```

```python
# Simplified _serialize_game:
if g.get('is_multi_disc'):
    s['is_multi_disc'] = True
    s['disc_count'] = g.get('disc_count', 0)
```

---

## Fix 3 (Medium): Restore cover path stashing for collection games

**File**: `decky_plugin/main.py:1812-1828`

When refactoring collection game serialization to add multi-disc detection, the cover path stashing was removed:
```python
if rid and rid not in idx and r.get('path_cover_small'):
    self._cover_paths[rid] = r.get('path_cover_small')
```

**Fix**: Re-add this block after appending the entry to `games`.

---

## Fix 4 (Medium): Remove `focusOnMount` from search TextField

**File**: `decky_plugin/src/index.tsx:2222-2226`

`focusOnMount` on `TextField` will pop the Steam virtual keyboard every time the Search tab is mounted (including every tab switch). This is disruptive.

**Fix**: Remove `focusOnMount` prop from the `TextField`. The search field should only open the keyboard when the user explicitly navigates to it and presses A (handled by the `Focusable` `onActivate` wrapper from Fix 1).

---

## Fix 5 (Low): Simplify `_serialize_game` elif branch

**File**: `decky_plugin/main.py:1779-1790`

The `elif` branch has an unnecessary `if g.get('disc_count'):` guard that drops `disc_count=0`.

**Fix**: After removing the filesystem call (Fix 2), this simplifies to:
```python
if g.get('is_multi_disc'):
    s['is_multi_disc'] = True
    s['disc_count'] = g.get('disc_count', 0)
```

---

## Fix 6 (Low): Replace namespace React import

**File**: `decky_plugin/src/index.tsx:18`

`import React` is used only for `React.forwardRef`.

**Fix**: Change to `import { forwardRef, ... }` and replace `React.forwardRef` with just `forwardRef`.

---

## Validation

1. `cd decky_plugin && npm run build` — no new TS errors beyond pre-existing TS7030 warning
2. Deploy to `~/homebrew/plugins/romm-sync-monitor/dist/` and `main.py`
3. Test on Steam Deck:
   - Multi-disc badge appears immediately on page load (no focus needed)
   - Disc count number shows on the badge
   - Search field is navigable via gamepad d-pad
   - Steam keyboard opens when pressing A on search field
   - Keyboard does NOT auto-open when switching to Search tab
   - Collection game covers still load correctly
