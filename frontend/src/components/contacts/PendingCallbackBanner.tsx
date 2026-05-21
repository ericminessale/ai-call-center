import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PhoneOutgoing, Clock, ChevronRight, UserCheck } from 'lucide-react';
import { callbacksApi, Callback } from '../../services/api';
import { useSocketContext } from '../../contexts/SocketContext';
import { logger } from '../../lib/logger';

// =============================================================================
// PendingCallbackBanner — shown atop the Contact detail when there's an
// outstanding callback for this contact.
//
// Distinguishes "pending" (unclaimed, waiting for any agent) from "claimed"
// (someone is actively working it). Click "Open" jumps to the Callbacks
// view with this row pre-selected and the appropriate filter pre-applied,
// so the agent never lands on an empty list.
// =============================================================================

interface PendingCallbackBannerProps {
  contactId: number;
}

export function PendingCallbackBanner({ contactId }: PendingCallbackBannerProps) {
  const [callback, setCallback] = useState<Callback | null>(null);
  const { socket } = useSocketContext();
  const navigate = useNavigate();

  // Poll once per contact change, then trust the socket for updates.
  useEffect(() => {
    let cancelled = false;
    callbacksApi
      .forContact(contactId)
      .then((res) => {
        if (!cancelled) setCallback(res.data.callback);
      })
      .catch((err) => logger.error('Failed to fetch pending callback', err));
    return () => {
      cancelled = true;
    };
  }, [contactId]);

  // Listen for any callback event involving this contact and refetch — keeps
  // the banner accurate when a new request arrives or someone marks one done.
  useEffect(() => {
    if (!socket) return;
    const handler = (data: { event: string; callback: Callback }) => {
      if (data.callback?.contactId === contactId) {
        callbacksApi
          .forContact(contactId)
          .then((res) => setCallback(res.data.callback))
          .catch(() => {});
      }
    };
    socket.on('callback_event', handler);
    return () => {
      socket.off('callback_event', handler);
    };
  }, [socket, contactId]);

  if (!callback) return null;

  const wait = callback.waitMinutes ?? 0;
  const reasonSummary = (callback.reason || '').slice(0, 90);
  const isClaimed = callback.status === 'claimed';

  // Color + label vary by lifecycle state. Pending stays orange (alerting,
  // someone needs to act); claimed flips to blue (informational, agent X is
  // already on it).
  const tone = isClaimed
    ? {
        border: 'border-blue-500/30',
        bg: 'bg-blue-500/10',
        iconBg: 'bg-blue-500/15',
        iconText: 'text-blue-300',
        label: 'text-blue-200',
        meta: 'text-blue-300/80',
        metaDim: 'text-blue-300/70',
        body: 'text-blue-100/80',
        btnBg: 'bg-blue-500/20 hover:bg-blue-500/30 text-blue-100',
        Icon: UserCheck,
      }
    : {
        border: 'border-orange-500/30',
        bg: 'bg-orange-500/10',
        iconBg: 'bg-orange-500/15',
        iconText: 'text-orange-300',
        label: 'text-orange-200',
        meta: 'text-orange-300/80',
        metaDim: 'text-orange-300/70',
        body: 'text-orange-100/80',
        btnBg: 'bg-orange-500/20 hover:bg-orange-500/30 text-orange-100',
        Icon: PhoneOutgoing,
      };

  const headline = isClaimed ? 'Callback in progress' : 'Awaiting callback';

  // Pass the callback id + suggested filter via router state so the
  // Callbacks page can pre-select the row and switch to a tab that
  // actually contains it.
  const handleOpen = () => {
    navigate('/callbacks', {
      state: {
        callbackId: callback.id,
        suggestedFilter: isClaimed ? 'mine' : 'pending',
      },
    });
  };

  return (
    <div className={`mt-4 rounded-lg border ${tone.border} ${tone.bg} px-4 py-3 flex items-center gap-3`}>
      <div className={`w-9 h-9 rounded-full flex items-center justify-center ${tone.iconBg} flex-shrink-0`}>
        <tone.Icon className={`w-4 h-4 ${tone.iconText}`} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-[13px] font-medium ${tone.label}`}>
            {headline}
          </span>
          <span className={`flex items-center gap-1 text-[11px] ${tone.meta}`}>
            <Clock className="w-2.5 h-2.5" />
            requested {formatWait(wait)} ago
          </span>
          {isClaimed && callback.claimedByAgentId !== null && (
            <span className={`text-[11px] ${tone.metaDim} font-mono`}>
              agent #{callback.claimedByAgentId}
            </span>
          )}
          {callback.attempts > 0 && (
            <span className={`text-[11px] ${tone.metaDim} font-mono`}>
              attempt #{callback.attempts + 1}
            </span>
          )}
        </div>
        {reasonSummary && (
          <p className={`text-[12px] ${tone.body} mt-0.5 truncate`}>
            {reasonSummary}
            {(callback.reason?.length ?? 0) > 90 ? '…' : ''}
          </p>
        )}
      </div>
      <button
        onClick={handleOpen}
        className={`flex items-center gap-1 px-3 py-1.5 text-[12px] rounded transition-colors flex-shrink-0 ${tone.btnBg}`}
      >
        Open
        <ChevronRight className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}

function formatWait(minutes: number): string {
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
}
