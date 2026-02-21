import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  TextField,
  Navigation,
  staticClasses,
  DialogButton,
} from "@decky/ui";
import { callable, definePlugin, toaster, routerHook, openFilePicker, FileSelectionType } from "@decky/api";
import { useState, useEffect, useRef, ChangeEvent } from "react";
import { FaSync, FaTrash, FaCog } from "react-icons/fa";
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
const enableRetroArchSetting = callable<[string], any>("enable_retroarch_setting");

const formatSpeed = (bytesPerSec: number): string => {
  if (bytesPerSec >= 1024 * 1024) return `${(bytesPerSec / (1024 * 1024)).toFixed(1)} MB/s`;
  if (bytesPerSec >= 1024) return `${(bytesPerSec / 1024).toFixed(0)} KB/s`;
  return `${bytesPerSec.toFixed(0)} B/s`;
};

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
  const [testResult, setTestResult] = useState<{success: boolean; message: string} | null>(null);

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
            } catch {}
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
            } catch {}
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
            } catch {}
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
  const [enablingWarning, setEnablingWarning] = useState<string | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const configuredRef = useRef<boolean | null>(null);
  const intervalRef = useRef<any>(null);
  const optimisticOverrides = useRef<Map<string, {auto_sync: boolean, sync_state: string, downloaded?: number, total?: number}>>(new Map());
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

  const handleEnableWarning = async (warningType: string) => {
    if (enablingWarning) return; // Already processing

    setEnablingWarning(warningType);
    try {
      const result = await enableRetroArchSetting(warningType);
      if (result?.success) {
        toaster.toast({
          title: '✅ Setting Enabled',
          body: result.message,
          duration: 4000,
        });
        // Refresh status to clear the warning
        await refreshStatus();
      } else {
        toaster.toast({
          title: '❌ Failed',
          body: result?.message || 'Could not enable setting',
          duration: 4000,
        });
      }
    } catch (error) {
      console.error('[ENABLE_WARNING] Error:', error);
      toaster.toast({
        title: '❌ Error',
        body: 'Failed to enable setting',
        duration: 4000,
      });
    } finally {
      setEnablingWarning(null);
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

      {/* RetroArch configuration warnings */}
      {status.details?.warnings && status.details.warnings.length > 0 && (
        <>
          {status.details.warnings.map((warning: any, index: number) => {
            const isEnabling = enablingWarning === warning.type;
            return (
              <div
                key={index}
                onClick={() => !isEnabling && handleEnableWarning(warning.type)}
                style={{
                  margin: '8px 0 4px',
                  padding: '10px 14px',
                  background: 'rgba(251, 191, 36, 0.12)',
                  border: '1px solid rgba(251, 191, 36, 0.4)',
                  borderRadius: '6px',
                  fontSize: '13px',
                  color: '#fbbf24',
                  lineHeight: '1.5',
                  cursor: isEnabling ? 'wait' : 'pointer',
                  opacity: isEnabling ? 0.6 : 1,
                  transition: 'opacity 0.2s, background 0.2s',
                }}
                onMouseEnter={(e) => {
                  if (!isEnabling) {
                    e.currentTarget.style.background = 'rgba(251, 191, 36, 0.2)';
                  }
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = 'rgba(251, 191, 36, 0.12)';
                }}
              >
                <strong>⚠️ RetroArch Setting:</strong> {warning.message}
                {!isEnabling && (
                  <div style={{ fontSize: '11px', marginTop: '4px', opacity: 0.8 }}>
                    Click to enable
                  </div>
                )}
                {isEnabling && (
                  <div style={{ fontSize: '11px', marginTop: '4px' }}>
                    Enabling...
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}

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
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <div style={{
                          width: '6px',
                          height: '6px',
                          borderRadius: '50%',
                          backgroundColor: getDotColor()
                        }} />
                        <span>{collection.name}</span>
                      </div>
                    }
                    description={(() => {
                      if (collection.auto_sync) {
                        return hasCount ? `${Math.floor(collection.downloaded)} / ${collection.total} ROMs` : "Fetching...";
                      }
                      return hasCount ? `${Math.floor(collection.downloaded)} / ${collection.total} ROMs locally` : "Fetching...";
                    })()}
                    checked={collection.auto_sync}
                    onChange={(value: boolean) => handleToggleCollection(collection.name, value)}
                    disabled={togglingCollection === collection.name}
                  />
                </PanelSectionRow>
                {isSyncing && hasCount && (
                  <PanelSectionRow>
                    <div style={{ width: '100%', padding: '0 2px 6px' }}>
                      {/* Progress bar */}
                      <div style={{
                        width: '100%',
                        height: '4px',
                        background: 'rgba(255,255,255,0.12)',
                        borderRadius: '2px',
                        overflow: 'hidden',
                        marginBottom: '5px',
                      }}>
                        <div style={{
                          width: `${pct}%`,
                          height: '100%',
                          background: '#fb923c',
                          borderRadius: '2px',
                          transition: 'width 0.4s ease',
                        }} />
                      </div>
                      {/* Percentage + speed row */}
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: '#9ca3af' }}>
                        <span>{pct}%</span>
                        {collection.speed > 0 && (
                          <span>{formatSpeed(collection.speed)}</span>
                        )}
                      </div>
                    </div>
                  </PanelSectionRow>
                )}
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