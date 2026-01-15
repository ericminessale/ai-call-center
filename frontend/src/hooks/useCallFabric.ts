/**
 * useCallFabric hook - wraps the CallFabricContext for backwards compatibility
 *
 * This hook provides the same interface as the old standalone implementation
 * but now uses the shared CallFabricContext under the hood.
 *
 * Benefits:
 * - Single shared client instance (no duplicate ICE gathering)
 * - Conference-based routing for outbound calls
 * - Consistent state across all components
 */

import { useCallFabricContext } from '../contexts/CallFabricContext';

export const useCallFabric = () => {
  const context = useCallFabricContext();

  // Map context values to the original hook interface for backwards compatibility
  return {
    // State
    client: context.client,
    activeCall: context.activeCall,
    isOnline: context.isOnline,
    isInitializing: context.isInitializing || context.isChangingStatus,
    error: context.error || context.conferenceJoinError,
    callState: context.callState,
    isMuted: context.isMuted,
    micPermission: context.micPermission,

    // Conference state (new)
    isInConference: context.isInConference,
    agentConference: context.agentConference,
    agentStatus: context.agentStatus,
    isClientReady: context.isClientReady,
    connectedCustomer: context.connectedCustomer,
    clearConnectedCustomer: context.clearConnectedCustomer,

    // Actions
    initializeClient: context.initializeClient,
    goOnline: async () => {
      // Going online now means going 'available' which joins the conference
      await context.setAgentStatus('available');
    },
    goOffline: async () => {
      await context.setAgentStatus('offline');
    },
    makeCall: context.makeCall,
    makeCallToSwml: context.makeCallToSwml,
    hangup: context.hangup,
    answerCall: context.answerCall,
    requestMicPermission: context.requestMicPermission,

    // Call controls
    mute: context.mute,
    unmute: context.unmute,
    hold: context.hold,
    unhold: context.unhold,
    sendDigits: context.sendDigits,

    // Status controls (new)
    setAgentStatus: context.setAgentStatus,
  };
};
