/**
 * ObserverControls — actions a user takes against a call they are NOT on.
 *
 * Listen (silent monitor), Whisper (coach the agent — supervisor is heard
 * only by the agent's leg), and Barge (full-audio join). Whisper/Barge are
 * conference-based actions so they only render for human calls; AI calls
 * have Takeover instead. Never render these on a call the user is
 * participating in — that's what CallControlPanel (participant) is for.
 *
 * Renders null when the user has no observer permission for this call type,
 * so the caller can embed it unconditionally in a row and let this component
 * decide visibility.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Ear, Megaphone, Mic } from 'lucide-react';
import { callControlApi } from '../../services/api';
import { usePermissions } from '../../hooks/usePermissions';
import { useCallFabricContext } from '../../contexts/CallFabricContext';
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

type ObserverMode = 'whisper' | 'barge';

export function ObserverControls({
  callId,
  callType,
  compact = false,
  onListenStarted,
  onListenStopped,
}: ObserverControlsProps) {
  const { can } = usePermissions();
  const { startObserverCall, stopObserverCall } = useCallFabricContext();
  const [listening, setListening] = useState(false);
  const [busy, setBusy] = useState(false);
  // Track monitor_type so we can wire the right audio transport. `tap` mounts
  // an AudioMonitor to stream the WebSocket audio; `conference` requires a
  // silent CF dial at the parent level (not wired in this pass).
  const [monitorType, setMonitorType] = useState<'tap' | 'conference' | null>(null);
  // Which observer join (whisper/barge) THIS instance started, if any.
  const [observerMode, setObserverMode] = useState<ObserverMode | null>(null);
  const observerModeRef = useRef<ObserverMode | null>(null);
  observerModeRef.current = observerMode;

  // Gate on the right flag for this call type. Agents see nothing by default.
  const listenFlag: PermissionFlag =
    callType === 'human' ? 'can_listen_human_calls' : 'can_listen_ai_calls';
  const canListen = can(listenFlag);
  // Whisper/Barge are conference joins — human-handled calls only.
  const canWhisper = callType === 'human' && can('can_whisper');
  const canBarge = callType === 'human' && can('can_barge');

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
          toast('Silent conference join not yet wired — tap-mode only for now', { icon: '⚠️' });
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

  const toggleObserver = useCallback(async (mode: ObserverMode) => {
    if (busy) return;
    setBusy(true);
    try {
      if (observerMode) {
        // Stop whichever join is active (the buttons are mutually exclusive).
        await stopObserverCall();
        setObserverMode(null);
      } else {
        const res = mode === 'whisper'
          ? await callControlApi.observeWhisper(callId)
          : await callControlApi.observeBarge(callId);
        await startObserverCall(res.data.dial_address);
        setObserverMode(mode);
        toast.success(mode === 'whisper'
          ? 'Whispering — only the agent hears you'
          : 'Barged in — everyone hears you');
      }
    } catch (e: unknown) {
      const message =
        (e as { response?: { data?: { error?: string } } })?.response?.data?.error ||
        `${mode === 'whisper' ? 'Whisper' : 'Barge'} failed`;
      toast.error(message);
    } finally {
      setBusy(false);
    }
  }, [busy, observerMode, callId, startObserverCall, stopObserverCall]);

  // If this instance started a whisper/barge and unmounts (row disappears,
  // view changes), hang the observer call up rather than leaving audio live.
  useEffect(() => () => {
    if (observerModeRef.current) {
      stopObserverCall().catch(() => {});
    }
  }, [stopObserverCall]);

  // No gate satisfied → render nothing. Caller doesn't need to branch.
  if (!canListen && !canWhisper && !canBarge) return null;

  const base =
    'inline-flex items-center gap-1.5 rounded-md font-medium transition-colors disabled:opacity-50';
  const size = compact ? 'px-2 py-1 text-[11px]' : 'px-3 py-1.5 text-xs';
  const idleState =
    'bg-canvas-raised text-ink-muted border border-rule hover:text-ink hover:border-rule-strong';
  const activeState = 'bg-canvas-hover text-ink border border-rule-strong';
  const iconSize = compact ? 'w-3 h-3' : 'w-3.5 h-3.5';

  return (
    <>
      {canListen && (
        <button
          type="button"
          onClick={toggleListen}
          disabled={busy}
          className={`${base} ${size} ${listening ? activeState : idleState}`}
          title={
            listening
              ? 'Stop listening to this call'
              : `Silently listen to this ${callType === 'ai' ? 'AI' : 'human'} call`
          }
        >
          <Ear className={iconSize} />
          {!compact && (busy ? '...' : listening ? 'Stop' : 'Listen')}
        </button>
      )}
      {canWhisper && (
        <button
          type="button"
          onClick={() => toggleObserver('whisper')}
          disabled={busy || (observerMode !== null && observerMode !== 'whisper')}
          className={`${base} ${size} ${observerMode === 'whisper' ? activeState : idleState}`}
          title={
            observerMode === 'whisper'
              ? 'Stop whispering'
              : 'Whisper to the agent — the caller cannot hear you'
          }
        >
          <Mic className={iconSize} />
          {!compact && (observerMode === 'whisper' ? 'Stop' : 'Whisper')}
        </button>
      )}
      {canBarge && (
        <button
          type="button"
          onClick={() => toggleObserver('barge')}
          disabled={busy || (observerMode !== null && observerMode !== 'barge')}
          className={`${base} ${size} ${observerMode === 'barge' ? activeState : idleState}`}
          title={
            observerMode === 'barge'
              ? 'Leave the call'
              : 'Barge into the call — everyone hears you'
          }
        >
          <Megaphone className={iconSize} />
          {!compact && (observerMode === 'barge' ? 'Leave' : 'Barge')}
        </button>
      )}
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
