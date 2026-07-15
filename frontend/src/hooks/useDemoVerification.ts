import { useEffect } from 'react';
import toast from 'react-hot-toast';
import { useAuthStore } from '../stores/authStore';
import { useVerifyStore } from '../stores/verifyStore';
import websocket from '../services/websocket';

/**
 * Initializes the shared phone-verification store for a hosted-demo visitor:
 * hydrates the current status once and flips it live when the inbound-SMS
 * webhook confirms the pairing (demo_phone_verified). Mount ONCE in the demo
 * shell (next to useDemoLeaseHeartbeat); all verification surfaces read
 * useVerifyStore.
 *
 * No-op outside demo mode and for platform operators (workspace_id null) —
 * the verify endpoints are visitor-only.
 */
export function useDemoVerification() {
  const isDemo = useAuthStore((s) => s.runtimeConfig?.demo_mode);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const isVisitor = useAuthStore((s) => s.user != null && s.user.workspace_id != null);

  useEffect(() => {
    if (!isDemo || !isAuthenticated || !isVisitor) return;

    void useVerifyStore.getState().hydrate();

    const onVerified = (data: { masked_number?: string }) => {
      const { verified } = useVerifyStore.getState();
      useVerifyStore.getState().markVerified(data?.masked_number ?? null);
      // Only celebrate the transition, not a redundant re-emit.
      if (!verified) {
        toast.success('Phone verified — call the demo number and your AI picks up');
      }
    };
    websocket.on('demo_phone_verified', onVerified);
    return () => {
      websocket.off('demo_phone_verified', onVerified);
    };
  }, [isDemo, isAuthenticated, isVisitor]);
}
