/**
 * ObserverControls — actions a user takes against a call they are NOT on.
 *
 * Listen (silent monitor) today. Whisper / Barge are scaffolded but hidden
 * until the backend endpoints exist. Never render these on a call the user
 * is participating in — that's what CallControlPanel (participant) is for.
 *
 * Renders null when the user has no observer permission for this call type,
 * so the caller can embed it unconditionally in a row and let this component
 * decide visibility.
 */
import { useCallback, useState } from 'react';
import { Ear } from 'lucide-react';
import { callControlApi } from '../../services/api';
import { usePermissions } from '../../hooks/usePermissions';
import type { PermissionFlag } from '../../types';
import { AudioMonitor } from './AudioMonitor';
import toast from 'react-hot-toast';

export interface ObserverControlsProps {
  callId: string | number;
  /** What kind of call this is — selects the permission flag to gate on. */
  callType: 'ai' | 'human';
  /** Compact = icon-only button for dense rows; default shows a label. */
  compact?: boolean;
  /** Fired after a successful listen-start, with the backend response body. */
  onListenStarted?: (response: unknown) => void;
  /** Fired after listen-stop. */
  onListenStopped?: () => void;
}

export function ObserverControls({
  callId,
  callType,
  compact = false,
  onListenStarted,
  onListenStopped,
}: ObserverControlsProps) {
  const { can } = usePermissions();
  const [listening, setListening] = useState(false);
  const [busy, setBusy] = useState(false);
  // Track monitor_type so we can wire the right audio transport. `tap` mounts
  // an AudioMonitor to stream the WebSocket audio; `conference` requires a
  // silent CF dial at the parent level (not wired in this pass).
  const [monitorType, setMonitorType] = useState<'tap' | 'conference' | null>(null);

  // Gate on the right flag for this call type. Agents see nothing by default.
  const listenFlag: PermissionFlag =
    callType === 'human' ? 'can_listen_human_calls' : 'can_listen_ai_calls';
  const canListen = can(listenFlag);

  const toggleListen = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (listening) {
        await callControlApi.stopMonitor(callId);
        setListening(false);
        setMonitorType(null);
        onListenStopped?.();
      } else {
        const res = await callControlApi.startMonitor(callId);
        const type = res.data?.monitor_type === 'conference' ? 'conference' : 'tap';
        setMonitorType(type);
        setListening(true);
        if (type === 'conference') {
          toast('Silent conference join not yet wired — tap-mode only for now', { icon: '\u26a0\ufe0f' });
        }
        onListenStarted?.(res.data);
      }
    } catch (e: unknown) {
      const message =
        (e as { response?: { data?: { error?: string } } })?.response?.data?.error ||
        'Listen action failed';
      toast.error(message);
    } finally {
      setBusy(false);
    }
  }, [busy, listening, callId, onListenStarted, onListenStopped]);

  // No gate satisfied → render nothing. Caller doesn't need to branch.
  if (!canListen) return null;

  const base =
    'inline-flex items-center gap-1.5 rounded-md font-medium transition-colors disabled:opacity-50';
  const size = compact ? 'px-2 py-1 text-[11px]' : 'px-3 py-1.5 text-xs';
  const state = listening
    ? 'bg-ai/20 text-ai-soft border border-ai/40 hover:bg-ai/25'
    : 'bg-canvas-raised text-ink-muted border border-rule hover:text-ink hover:border-rule-strong';

  return (
    <>
      <button
        type="button"
        onClick={toggleListen}
        disabled={busy}
        className={`${base} ${size} ${state}`}
        title={
          listening
            ? 'Stop listening to this call'
            : `Silently listen to this ${callType === 'ai' ? 'AI' : 'human'} call`
        }
      >
        <Ear className={compact ? 'w-3 h-3' : 'w-3.5 h-3.5'} />
        {!compact && (busy ? '...' : listening ? 'Stop' : 'Listen')}
      </button>
      {listening && monitorType === 'tap' && (
        <AudioMonitor
          callId={String(callId)}
          onClose={() => {
            setListening(false);
            setMonitorType(null);
            callControlApi.stopMonitor(callId).catch(() => {});
            onListenStopped?.();
          }}
        />
      )}
    </>
  );
}

export default ObserverControls;
