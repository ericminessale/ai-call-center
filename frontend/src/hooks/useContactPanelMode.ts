/**
 * useContactPanelMode — derive what the contact-detail panel should render
 * from the agent's actual session state, not from the inbound Call row's
 * mutable status field.
 *
 * Why this exists:
 *
 * The contact panel used to gate AI-vs-human controls on
 * `activeCallForContact.status === 'ai_active' || handler_type === 'ai'`.
 * That field is the customer's call row — written by webhook chains
 * (conference-join, status-callback, AI post-prompt) — and lags the agent's
 * actual leg state. Every time we add a new join path we have to remember
 * to flip the row at exactly the right moment, and panel-mode bugs
 * resurface ("agent has a live human leg but UI still shows AI controls").
 *
 * The agent's SDK already knows the truth: `callState`, `activeCall`,
 * `pendingCallAssignment`. This hook treats the SDK as the authoritative
 * source for "what is the agent doing right now" and uses the inbound Call
 * row only as metadata (queue, AI context, contact linkage).
 *
 * Rule of thumb when wiring new UI: if it's something the AGENT is doing
 * (mute, hold, end-call, take-over), gate it on this hook. If it's
 * metadata ABOUT the call (queue label, AI context card, wait time, AI
 * summary), read from `activeCallForContact` directly.
 */

import { useCallFabricContext } from '../contexts/CallFabricContext';
import type { Call } from '../types/callcenter';

export type ContactPanelMode =
  /** No agent leg, no inbound call for this contact. Show "Call" / "Send AI" buttons. */
  | { kind: 'idle' }
  /** Contact has a call sitting in queue; agent hasn't accepted yet. Show "Take call" banner. */
  | { kind: 'queued_pretake'; call: Call }
  /**
   * Contact has an AI-handled call and the agent is NOT on it. This is the
   * supervisor / browse view — show listen/whisper/send-system-message
   * monitor surface, but no end/hold/mute (those are the agent's controls
   * and the viewing agent isn't on the leg).
   */
  | { kind: 'ai_monitor'; call: Call }
  /**
   * The agent has a live SDK leg (callState='active' OR `activeCall` set).
   * This is the human-controls mode regardless of what the customer's Call
   * row says — once the SDK is in 'active', the agent IS on a human leg
   * even if the customer-side row hasn't been webhook-updated yet.
   */
  | { kind: 'human_active'; call?: Call }
  /**
   * Agent's SDK is dialing out, ringing, or has a pending assignment. UI
   * shows "Calling…" / "Ringing…" / accept banner, no end/hold yet.
   */
  | { kind: 'outbound_ringing'; call?: Call };

/**
 * Convenience: most JSX gates only care about a few buckets. Destructure
 * these instead of pattern-matching `mode.kind` everywhere.
 */
export interface PanelModeFlags {
  mode: ContactPanelMode;
  /** Agent has (or is establishing) a live leg. Drives the "call active" header. */
  isAgentOnCall: boolean;
  /** Show full human controls (mute / hold / record / end). */
  showHumanControls: boolean;
  /**
   * Show AI-mode monitor surface (listen, send-system-message). Mutually
   * exclusive with showHumanControls — if the agent has joined an
   * AI-routed call, they're now human-handling it.
   */
  showAIMonitor: boolean;
  /** Show the "Take call" pre-pickup banner. */
  showTakeBanner: boolean;
  /** Show the "Calling…/Ringing…" pill. */
  showRinging: boolean;
}

/**
 * Compute the panel mode from the agent's SDK session and (optionally) the
 * inbound Call row for the currently-viewed contact.
 *
 * Order of precedence is deliberate — the SDK wins. If `callState='active'`
 * the panel is human-active regardless of the inbound row's status. The
 * inbound row only gets consulted when there's no agent session to lean on.
 */
export function useContactPanelMode(
  contactInboundCall: Call | undefined | null,
): PanelModeFlags {
  const { callState, activeCall, pendingCallAssignment } = useCallFabricContext();

  const mode = deriveMode(
    { callState, hasActiveCall: !!activeCall, hasPendingAssignment: !!pendingCallAssignment },
    contactInboundCall ?? undefined,
  );

  return {
    mode,
    isAgentOnCall: mode.kind === 'human_active' || mode.kind === 'outbound_ringing',
    showHumanControls: mode.kind === 'human_active',
    showAIMonitor: mode.kind === 'ai_monitor',
    showTakeBanner: mode.kind === 'queued_pretake',
    showRinging: mode.kind === 'outbound_ringing',
  };
}

/**
 * Pure helper — extracted so the precedence logic is testable in isolation
 * without needing to mount a CallFabricProvider.
 */
export function deriveMode(
  sdk: {
    callState: 'idle' | 'ringing' | 'active' | 'ending';
    hasActiveCall: boolean;
    hasPendingAssignment: boolean;
  },
  contactCall: Call | undefined,
): ContactPanelMode {
  // Rule 1 — SDK wins. If the agent has a live leg, the panel is human-mode.
  // This is the rule that fixes the "ai_active stuck in UI after handoff"
  // class of bugs: it short-circuits BEFORE we look at any DB-row status.
  if (sdk.callState === 'active' || sdk.hasActiveCall) {
    return { kind: 'human_active', call: contactCall };
  }

  // Rule 2 — SDK is mid-transition. Show ringing/calling UI.
  if (sdk.callState === 'ringing' || sdk.callState === 'ending' || sdk.hasPendingAssignment) {
    return { kind: 'outbound_ringing', call: contactCall };
  }

  // Rule 3 — No agent session. The inbound Call row (if any) decides.
  if (!contactCall) return { kind: 'idle' };

  const PRE_TAKE = new Set(['waiting', 'assigned', 'queued', 'urgent']);
  if (PRE_TAKE.has(contactCall.status || '')) {
    return { kind: 'queued_pretake', call: contactCall };
  }

  // Rule 4 — Contact has an AI call in flight and the viewing agent is NOT on
  // it. This is the ONLY place where the inbound row's AI markers drive
  // panel mode, and it's a supervisor/observer surface, not the call-owner
  // surface. Once the agent accepts and the SDK fires call.joined, Rule 1
  // takes over and this branch becomes unreachable.
  if (contactCall.status === 'ai_active' || contactCall.handler_type === 'ai') {
    return { kind: 'ai_monitor', call: contactCall };
  }

  return { kind: 'idle' };
}
