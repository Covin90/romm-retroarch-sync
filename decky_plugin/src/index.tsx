import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  ToggleField,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useState, useEffect, useRef } from "react";
import { FaSync, FaTrash } from "react-icons/fa";

// Call backend methods
const getServiceStatus = callable<[], any>("get_service_status");
const startService = callable<[], boolean>("start_service");
const stopService = callable<[], boolean>("stop_service");
const toggleCollectionSync = callable<[string, boolean], boolean>("toggle_collection_sync");
const deleteCollectionRoms = callable<[string], boolean>("delete_collection_roms");

function Content() {
  const [status, setStatus] = useState<any>({ status: 'loading', message: 'Loading...' });
  const [loading, setLoading] = useState(false);
  const [togglingCollection, setTogglingCollection] = useState<string | null>(null);
  const intervalRef = useRef<any>(null);
  const optimisticOverrides = useRef<Map<string, {auto_sync: boolean, sync_state: string}>>(new Map());

  const refreshStatus = async () => {
    try {
      const result = await getServiceStatus();
      console.log(`[REFRESH] Got status, overrides:`, Array.from(optimisticOverrides.current.keys()));

      // Check if backend data matches any overrides - if so, clear them
      if (optimisticOverrides.current.size > 0) {
        result.collections.forEach((col: any) => {
          const override = optimisticOverrides.current.get(col.name);
          // Clear override only when BOTH auto_sync and sync_state match
          if (override && override.auto_sync === col.auto_sync &&
              (override.sync_state === col.sync_state || col.sync_state === 'synced')) {
            console.log(`[REFRESH] Backend matches override for ${col.name}, clearing override`);
            optimisticOverrides.current.delete(col.name);
          }
        });
      }

      // Apply remaining optimistic overrides (only override auto_sync, keep progress data)
      if (optimisticOverrides.current.size > 0) {
        console.log(`[REFRESH] Applying overrides to collections`);
        const modifiedResult = {
          ...result,
          collections: result.collections.map((col: any) => {
            const override = optimisticOverrides.current.get(col.name);
            if (override) {
              console.log(`[REFRESH] Overriding auto_sync and sync_state for ${col.name}, keeping progress`);
              // Override both auto_sync and sync_state to prevent red intermediate state
              // Keep downloaded/total updating normally for real-time progress
              return { ...col, auto_sync: override.auto_sync, sync_state: override.sync_state };
            }
            return col;
          })
        };
        setStatus(modifiedResult);
      } else {
        console.log(`[REFRESH] No overrides, using raw result`);
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
    intervalRef.current = setInterval(refreshStatus, 5000);
  };

  useEffect(() => {
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

    // FIRST: Set override BEFORE anything else to protect against concurrent polling
    optimisticOverrides.current.set(collectionName, {
      auto_sync: enabled,
      sync_state: enabled ? 'syncing' : 'not_synced'
    });
    console.log(`[TOGGLE] Set override for ${collectionName}, map size:`, optimisticOverrides.current.size);

    setTogglingCollection(collectionName);

    // Update UI immediately - change auto_sync and set initial sync_state
    setStatus((prevStatus: any) => {
      const updatedCollections = prevStatus.collections.map((col: any) => {
        if (col.name === collectionName) {
          // When enabling, show as syncing; when disabling, show as not_synced
          const newSyncState = enabled ? 'syncing' : 'not_synced';
          return { ...col, auto_sync: enabled, sync_state: newSyncState };
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
      // Override will auto-clear when backend data matches (via refreshStatus)
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
                    description={
                      collection.auto_sync
                        ? collection.downloaded !== undefined && collection.total !== undefined
                          ? `${collection.downloaded} / ${collection.total} ROMs`
                          : "Auto-sync enabled"
                        : "Auto-sync disabled"
                    }
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
                      <span>Delete ROMs & Re-sync</span>
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
          🔄 Refresh
        </ButtonItem>
      </PanelSectionRow>

      {status.status === 'stopped' && (
        <PanelSectionRow>
          <ButtonItem 
            layout="below" 
            onClick={handleStart}
            disabled={loading}
          >
            ▶️ Start Service
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
            ⏹️ Stop Service
          </ButtonItem>
        </PanelSectionRow>
      )}
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
  return {
    name: "RomM Sync Monitor",
    titleView: <TitleView />,
    content: <Content />,
    icon: <FaSync />,
  };
});