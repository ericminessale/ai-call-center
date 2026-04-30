import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PhoneOutgoing, Clock, ChevronRight } from 'lucide-react';
import { callbacksApi, Callback } from '../../services/api';
import { useSocketContext } from '../../contexts/SocketContext';
import { logger } from '../../lib/logger';

// =============================================================================
// PendingCallbackBanner — shown atop the Contact detail when there's a
// pending callback for this contact (Tier 2r).
//
// Click "Open" to jump straight to the Callbacks view with this row
// pre-selected; agents don't have to hunt through the queue list.
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

  return (
    <div className="mt-4 rounded-lg border border-orange-500/30 bg-orange-500/10 px-4 py-3 flex items-center gap-3">
      <div className="w-9 h-9 rounded-full flex items-center justify-center bg-orange-500/15 flex-shrink-0">
        <PhoneOutgoing className="w-4 h-4 text-orange-300" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[13px] font-medium text-orange-200">
            Awaiting callback
          </span>
          <span className="flex items-center gap-1 text-[11px] text-orange-300/80">
            <Clock className="w-2.5 h-2.5" />
            requested {formatWait(wait)} ago
          </span>
          {callback.attempts > 0 && (
            <span className="text-[11px] text-orange-300/70 font-mono">
              attempt #{callback.attempts + 1}
            </span>
          )}
        </div>
        {reasonSummary && (
          <p className="text-[12px] text-orange-100/80 mt-0.5 truncate">
            {reasonSummary}
            {(callback.reason?.length ?? 0) > 90 ? '…' : ''}
          </p>
        )}
      </div>
      <button
        onClick={() => navigate('/callbacks')}
        className="flex items-center gap-1 px-3 py-1.5 text-[12px] rounded bg-orange-500/20 hover:bg-orange-500/30 text-orange-100 transition-colors flex-shrink-0"
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
