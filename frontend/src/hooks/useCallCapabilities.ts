/**
 * useCallCapabilities — read the per-call capability set off a Call payload.
 *
 * The backend is the source of truth: `Call.to_dict()` calls
 * `app.services.call_transport.capabilities()` and ships the resulting set as
 * the `capabilities` string list on every Call payload. Add a new capability
 * to the backend and the UI lights up automatically.
 *
 * Usage:
 *   const caps = useCallCapabilities(call);
 *   if (caps.can('hold')) return <HoldButton/>;
 *
 * Or destructure handy booleans:
 *   const { canHold, canBarge, canMonitor } = useCallCapabilities(call);
 *
 * See CALL_TRANSPORT.md for the full capability matrix.
 */
import { useMemo } from 'react';
import type { Call, CallCapability } from '../types/callcenter';

export interface CallCapabilities {
  /** Raw set, lower-level escape hatch. */
  set: Set<CallCapability>;
  /** Predicate lookup. */
  can: (cap: CallCapability) => boolean;
  /** Pre-computed booleans for the buttons we actually render today. */
  canHold: boolean;
  canUnhold: boolean;
  canSendDtmfCaller: boolean;
  canSendDtmfAgent: boolean;
  canRecordStart: boolean;
  canRecordStop: boolean;
  canMonitor: boolean;
  canWhisper: boolean;
  canBarge: boolean;
  canTakeover: boolean;
  canTransfer: boolean;
  canLiveTranslate: boolean;
  canSidecarCoach: boolean;
  /** Convenience: this call is in a transport that supports any multi-party
   *  surface (whisper/barge/monitor). Used by ConferenceParticipants and
   *  the supervisor observer UI. */
  isMultiPartyCapable: boolean;
}

/** Sentinel "everything off" fallback. Used when the Call object hasn't
 *  loaded yet or the payload predates the M2 wire format. */
const EMPTY: CallCapabilities = {
  set: new Set(),
  can: () => false,
  canHold: false,
  canUnhold: false,
  canSendDtmfCaller: false,
  canSendDtmfAgent: false,
  canRecordStart: false,
  canRecordStop: false,
  canMonitor: false,
  canWhisper: false,
  canBarge: false,
  canTakeover: false,
  canTransfer: false,
  canLiveTranslate: false,
  canSidecarCoach: false,
  isMultiPartyCapable: false,
};

/** Legacy fallback for calls predating the M0+M1 wire format (no `capabilities`
 *  field). Default to the conference set so existing UIs don't suddenly hide
 *  buttons that used to render. Once all live calls flow through the new
 *  pipeline this branch is dead. */
const LEGACY_CONFERENCE: CallCapability[] = [
  'hold', 'unhold', 'record_start', 'record_stop',
  'monitor_listen', 'whisper', 'barge',
  'takeover', 'transfer',
  'live_translate', 'sidecar_coach',
];

export function useCallCapabilities(
  call: Call | null | undefined,
): CallCapabilities {
  return useMemo(() => {
    if (!call) return EMPTY;

    const raw: string[] = Array.isArray(call.capabilities)
      ? call.capabilities
      // Pre-M2 calls won't have the field. Fall back to the conference
      // capability set so we don't accidentally hide working buttons during
      // the rollout window.
      : LEGACY_CONFERENCE;

    const set = new Set(raw as CallCapability[]);
    const has = (c: CallCapability) => set.has(c);

    return {
      set,
      can: has,
      canHold: has('hold'),
      canUnhold: has('unhold'),
      canSendDtmfCaller: has('send_dtmf_caller'),
      canSendDtmfAgent: has('send_dtmf_agent'),
      canRecordStart: has('record_start'),
      canRecordStop: has('record_stop'),
      canMonitor: has('monitor_listen'),
      canWhisper: has('whisper'),
      canBarge: has('barge'),
      canTakeover: has('takeover'),
      canTransfer: has('transfer'),
      canLiveTranslate: has('live_translate'),
      canSidecarCoach: has('sidecar_coach'),
      isMultiPartyCapable:
        has('monitor_listen') || has('whisper') || has('barge'),
    };
  }, [call]);
}
