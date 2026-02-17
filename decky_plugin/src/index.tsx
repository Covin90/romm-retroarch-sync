import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  TextField,
  Navigation,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster, routerHook } from "@decky/api";
import { useState, useEffect, useRef, ChangeEvent } from "react";
import { FaSync, FaTrash, FaCog } from "react-icons/fa";

// Call backend methods
const getServiceStatus = callable<[], any>("get_service_status");
const startService = callable<[], boolean>("start_service");
const stopService = callable<[], boolean>("stop_service");
const toggleCollectionSync = callable<[string, boolean], boolean>("toggle_collection_sync");
const deleteCollectionRoms = callable<[string], boolean>("delete_collection_roms");
const getLoggingEnabled = callable<[], boolean>("get_logging_enabled");
const updateLoggingEnabled = callable<[boolean], boolean>("set_logging_enabled");
const getConfig = callable<[], any>("get_config");
const saveConfig = callable<[string, string, string, string, string, string], any>("save_config");
const testRommConnection = callable<[string, string, string], any>("test_connection");

// Background monitoring - runs independently of UI
let backgroundInterval: any = null;
const previousSyncStates = new Map<string, {sync_state: string, downloaded?: number, total?: number}>();

const checkForNotifications = async () => {
  try {
    const result = await getServiceStatus();

    // Collect all collections that need notifications
    const notificationsToShow: Array<{name: string, downloaded: number, total: number, type: string}> = [];

    // Detect sync completion
    result.collections?.forEach((col: any) => {
      const previousData = previousSyncStates.get(col.name);
      const currentState = col.sync_state;

      // Log state for debugging
      if (col.auto_sync || previousData) {
        console.log(`[BACKGROUND CHECK] ${col.name}: prev=${previousData?.sync_state || 'none'}, curr=${currentState}, auto_sync=${col.auto_sync}`);
      }

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
  const [deviceName, setDeviceName] = useState('');
  const [hasPassword, setHasPassword] = useState(false);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<{success: boolean; message: string} | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const config = await getConfig();
        setUrl(config.url || '');
        setUsername(config.username || '');
        setRomDir(config.rom_directory || '');
        setSaveDir(config.save_directory || '');
        setDeviceName(config.device_name || '');
        setHasPassword(config.has_password || false);
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

  const handleSave = async () => {
    setSaving(true);
    try {
      const result = await saveConfig(url.trim(), username.trim(), password, romDir.trim(), saveDir.trim(), deviceName.trim());
      if (result.success) {
        toaster.toast({ title: 'RomM Sync', body: 'Configuration saved — daemon restarted!', duration: 3000 });
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
    <div style={{ marginTop: '40px', color: 'white' }}>
      <div className={staticClasses.Title} style={{ marginBottom: '20px' }}>RomM Connection Setup</div>

      <PanelSection title="Server">
        <PanelSectionRow>
          <TextField
            label="RomM URL"
            value={url}
            onChange={(e: ChangeEvent<HTMLInputElement>) => { setUrl(e.target.value); setTestResult(null); }}
            description="e.g. https://romm.example.com"
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Credentials">
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
          <TextField
            label="Save Directory"
            value={saveDir}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setSaveDir(e.target.value)}
            description="Where save files are stored"
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Device">
        <PanelSectionRow>
          <TextField
            label="Device Name"
            value={deviceName}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setDeviceName(e.target.value)}
            description="Name shown in RomM for this device"
          />
        </PanelSectionRow>
      </PanelSection>

      {testResult && (
        <PanelSection>
          <PanelSectionRow>
            <div style={{ color: testResult.success ? '#4ade80' : '#f87171', fontSize: '0.9em', padding: '4px 0' }}>
              {testResult.success ? '✅' : '❌'} {testResult.message}
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleTest} disabled={testing || saving || !url.trim() || !username.trim()}>
            {testing ? 'Testing…' : '🔌 Test Connection'}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleSave} disabled={saving || testing || !canSubmit}>
            {saving ? 'Saving…' : '💾 Save & Apply'}
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
function SettingsPage() {
  const [loggingEnabled, setLoggingEnabled] = useState<boolean>(true);
  const [loading, setLoading] = useState<boolean>(true);

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
      // Revert on error
      setLoggingEnabled(!enabled);
    }
  };

  return (
    <div style={{ marginTop: "40px", color: "white" }}>
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
    </div>
  );
}

function Content() {
  const [status, setStatus] = useState<any>({ status: 'loading', message: 'Loading...' });
  const [loading, setLoading] = useState(false);
  const [togglingCollection, setTogglingCollection] = useState<string | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const intervalRef = useRef<any>(null);
  const optimisticOverrides = useRef<Map<string, {auto_sync: boolean, sync_state: string, downloaded?: number, total?: number}>>(new Map());

  const refreshStatus = async () => {
    try {
      const result = await getServiceStatus();
      console.log(`[REFRESH] ========== NEW REFRESH ==========`);
      console.log(`[REFRESH] Raw backend data:`, JSON.stringify(result.collections, null, 2));
      console.log(`[REFRESH] Current overrides:`, Array.from(optimisticOverrides.current.entries()));

      // Check if backend data matches any overrides - if so, clear them
      if (optimisticOverrides.current.size > 0) {
        result.collections.forEach((col: any) => {
          const override = optimisticOverrides.current.get(col.name);
          if (override) {
            console.log(`[REFRESH] Checking ${col.name}: override={auto_sync:${override.auto_sync}, sync_state:${override.sync_state}}, backend={auto_sync:${col.auto_sync}, sync_state:${col.sync_state}, downloaded:${col.downloaded}, total:${col.total}}`);
          }
          if (override && override.auto_sync === col.auto_sync) {
            // Clear override when backend has REAL progress data (not just 0/0)
            const backendHasProgress = (col.downloaded !== undefined && col.total !== undefined && col.total > 0);
            const shouldClear = backendHasProgress && (
              // Clear if backend state matches override state
              override.sync_state === col.sync_state ||
              // OR clear if backend is 'synced' (sync completed, override no longer needed)
              col.sync_state === 'synced'
            );
            console.log(`[REFRESH] ${col.name}: shouldClear=${shouldClear} (backendHasProgress=${backendHasProgress}, override.sync_state=${override.sync_state}, col.sync_state=${col.sync_state})`);
            if (shouldClear) {
              console.log(`[REFRESH] ✅ CLEARING override for ${col.name}`);
              optimisticOverrides.current.delete(col.name);
            } else {
              console.log(`[REFRESH] ⏸️  KEEPING override for ${col.name} (backend has no progress yet)`);
            }
          }
        });
      }

      // Apply remaining optimistic overrides
      if (optimisticOverrides.current.size > 0) {
        console.log(`[REFRESH] 🎨 Applying ${optimisticOverrides.current.size} override(s) to result`);
        const modifiedResult = {
          ...result,
          collections: result.collections.map((col: any) => {
            const override = optimisticOverrides.current.get(col.name);
            if (override) {
              const before = `{sync_state:${col.sync_state}, downloaded:${col.downloaded}, total:${col.total}}`;
              // Apply override fields - if override has downloaded/total, use them
              const modified = {
                ...col,
                auto_sync: override.auto_sync,
                sync_state: override.sync_state,
                ...(override.downloaded !== undefined ? { downloaded: override.downloaded } : {}),
                ...(override.total !== undefined ? { total: override.total } : {})
              };
              const after = `{sync_state:${modified.sync_state}, downloaded:${modified.downloaded}, total:${modified.total}}`;
              console.log(`[REFRESH] 🔧 ${col.name}: BEFORE=${before}, AFTER=${after}`);
              return modified;
            }
            return col;
          })
        };
        console.log(`[REFRESH] Final data being set to UI:`, JSON.stringify(modifiedResult.collections, null, 2));
        setStatus(modifiedResult);
      } else {
        console.log(`[REFRESH] No overrides, using raw backend result`);
        console.log(`[REFRESH] Final data being set to UI:`, JSON.stringify(result.collections, null, 2));
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
    getConfig().then((cfg: any) => setConfigured(cfg?.configured ?? false)).catch(() => setConfigured(false));
    refreshStatus();
    startPolling();
    return () => stopPolling();
  }, []);

  const handleStart = async () => {
    setLoading(true);
    await startService();
    setTimeout(refreshStatus, 1000); // Refresh after 1 second
    setLoading(false);
  };

  const handleStop = async () => {
    setLoading(true);
    await stopService();
    setTimeout(refreshStatus, 1000);
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
      // Force immediate refresh to get the fresh status from backend
      // The backend immediately updates status.json, so we should read it ASAP
      console.log(`[TOGGLE] Forcing immediate refresh after backend call`);
      await refreshStatus();
    } catch (error) {
      console.error('[TOGGLE] Failed to toggle collection sync:', error);
      // Clear override on error and refresh
      optimisticOverrides.current.delete(collectionName);
      refreshStatus();
    } finally {
      setTogglingCollection(null);
    }
  };

  const handleDeleteCollection = async (collectionName: string) => {
    console.log(`[DELETE] Starting deletion for ${collectionName}`);
    setTogglingCollection(collectionName);

    // Store optimistic override
    optimisticOverrides.current.set(collectionName, {
      auto_sync: false,
      sync_state: 'not_synced'
    });

    // Update UI immediately - only change auto_sync
    setStatus((prevStatus: any) => {
      const updatedCollections = prevStatus.collections.map((col: any) =>
        col.name === collectionName
          ? { ...col, auto_sync: false }
          : col
      );
      return {
        ...prevStatus,
        collections: updatedCollections,
        actively_syncing_count: updatedCollections.filter((c: any) => c.auto_sync).length
      };
    });

    try {
      const result = await deleteCollectionRoms(collectionName);
      console.log(`[DELETE] Backend returned:`, result);
      // Override will auto-clear when backend data matches (via refreshStatus)
    } catch (error) {
      console.error('[DELETE] Failed to delete collection ROMs:', error);
      optimisticOverrides.current.delete(collectionName);
      refreshStatus();
    } finally {
      setTogglingCollection(null);
    }
  };

  if (configured === false) {
    return (
      <PanelSection title="RomM Sync">
        <PanelSectionRow>
          <div style={{ color: '#fbbf24', fontSize: '0.9em', marginBottom: '8px' }}>
            ⚙️ RomM is not configured yet. Set up your server connection to get started.
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
              <span>Set Up RomM Connection</span>
            </div>
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <PanelSection title="RomM Sync Status">
      <PanelSectionRow>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span>{status.message}</span>
        </div>
      </PanelSectionRow>

      {status.collections && status.collections.length > 0 && (
        <>
          <PanelSectionRow>
            <div style={{ fontSize: '0.9em', color: '#b0b0b0' }}>
              Collections:
            </div>
          </PanelSectionRow>
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

            return (
              <div key={index}>
                <PanelSectionRow>
                  <ToggleField
                    label={
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <div style={{
                          width: '6px',
                          height: '6px',
                          borderRadius: '50%',
                          backgroundColor: getDotColor()
                        }} />
                        <span>
                          {collection.name}
                          {collection.auto_sync && collection.sync_state === 'syncing' ? ' - Syncing' : ''}
                        </span>
                      </div>
                    }
                    description={(() => {
                      console.log(`[RENDER] ${collection.name}: auto_sync=${collection.auto_sync}, downloaded=${collection.downloaded}, total=${collection.total}, sync_state=${collection.sync_state}`);
                      if (collection.auto_sync) {
                        // Only show counts if total > 0 (valid data from server)
                        if (collection.downloaded !== undefined && collection.total !== undefined && collection.total > 0) {
                          return `${collection.downloaded} / ${collection.total} ROMs`;
                        }
                        return "Auto-sync enabled";
                      }
                      return "Auto-sync disabled";
                    })()}
                    checked={collection.auto_sync}
                    onChange={(value: boolean) => handleToggleCollection(collection.name, value)}
                    disabled={togglingCollection === collection.name}
                  />
                </PanelSectionRow>
                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    onClick={() => handleDeleteCollection(collection.name)}
                    disabled={togglingCollection === collection.name}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <FaTrash size={12} />
                      <span>Delete ROMs</span>
                    </div>
                  </ButtonItem>
                </PanelSectionRow>
              </div>
            );
          })}
        </>
      )}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={refreshStatus}
          disabled={loading}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <span>🔄</span>
            <span>Refresh</span>
          </div>
        </ButtonItem>
      </PanelSectionRow>
      {status.status === 'stopped' && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={handleStart}
            disabled={loading}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span>▶️</span>
              <span>Start Service</span>
            </div>
          </ButtonItem>
        </PanelSectionRow>
      )}
      {status.status !== 'stopped' && status.status !== 'error' && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={handleStop}
            disabled={loading}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span>⏹️</span>
              <span>Stop Service</span>
            </div>
          </ButtonItem>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => {
            Navigation.Navigate("/romm-sync-settings");
            Navigation.CloseSideMenus();
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <FaCog size={14} />
            <span>Settings</span>
          </div>
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function TitleView() {
  const [status, setStatus] = useState<any>({ status: 'loading' });

  useEffect(() => {
    const fetchStatus = async () => {
      const result = await getServiceStatus();
      setStatus(result);
    };
    fetchStatus();
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, []);

  // Status dot colors
  const getStatusColor = () => {
    switch (status.status) {
      case 'connected': return '#4ade80'; // green
      case 'running': return '#fbbf24'; // yellow
      case 'service_only': return '#60a5fa'; // blue
      case 'stopped': return '#f87171'; // red
      case 'error': return '#f87171'; // red
      default: return '#9ca3af'; // gray
    }
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      <div style={{
        width: '8px',
        height: '8px',
        borderRadius: '50%',
        backgroundColor: getStatusColor()
      }} />
      <span>RomM Sync</span>
    </div>
  );
}

export default definePlugin(() => {
  routerHook.addRoute("/romm-sync-settings", () => <SettingsPage />, { exact: true });
  routerHook.addRoute("/romm-sync-config", () => <ConfigPage />, { exact: true });

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
    },
  };
});