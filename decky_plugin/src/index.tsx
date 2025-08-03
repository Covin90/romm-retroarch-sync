import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useState, useEffect } from "react";
import { FaCircle } from "react-icons/fa";

// Call backend methods
const getServiceStatus = callable<[], any>("get_service_status");
const startService = callable<[], boolean>("start_service");
const stopService = callable<[], boolean>("stop_service");

function Content() {
  const [status, setStatus] = useState<any>({ status: 'loading', message: 'Loading...' });
  const [loading, setLoading] = useState(false);

  const refreshStatus = async () => {
    try {
      const result = await getServiceStatus();
      setStatus(result);
    } catch (error) {
      setStatus({ status: 'error', message: '❌ Plugin error' });
    }
  };

  useEffect(() => {
    refreshStatus();
    // Refresh every 5 seconds
    const interval = setInterval(refreshStatus, 5000);
    return () => clearInterval(interval);
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

  const getStatusColor = () => {
    switch (status.status) {
      case 'connected': return '#00ff00';
      case 'running': return '#ffff00';
      case 'service_only': return '#0080ff';
      case 'stopped': return '#ff0000';
      default: return '#808080';
    }
  };

  return (
    <PanelSection title="RomM Sync Status">
      <PanelSectionRow>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <FaCircle color={getStatusColor()} size={12} />
          <span>{status.message}</span>
        </div>
      </PanelSectionRow>
      
      {status.details?.game_count && (
        <PanelSectionRow>
          <div style={{ fontSize: '0.9em', color: '#b0b0b0' }}>
            Games: {status.details.game_count}
          </div>
        </PanelSectionRow>
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

export default definePlugin(() => {
  return {
    name: "RomM Sync Monitor",
    title: <div className={staticClasses.Title}>RomM Sync</div>,
    content: <Content />,
    icon: <FaCircle />,
  };
});