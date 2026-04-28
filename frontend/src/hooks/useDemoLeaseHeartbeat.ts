import { useEffect } from 'react';
import { demoApi } from '../services/api';
import { useAuthStore } from '../stores/authStore';
import { logger } from '../lib/logger';

/**
 * Keep-alive for the visitor's demo persona lease.
 *
 * While a demo-mode visitor is signed in, the backend's lease TTL
 * decays unless we ping ``/api/demo/heartbeat`` regularly. We send a
 * heartbeat every 60s — well under the 300s default TTL, comfortable
 * for a network blip.
 *
 * On ``beforeunload`` we fire-and-forget ``/api/demo/end`` so the
 * lease releases immediately rather than waiting for the TTL to
 * expire. ``navigator.sendBeacon`` is the right primitive — survives
 * the page unload that would normally cancel a fetch.
 *
 * Both behaviors are no-ops outside demo mode. Mount this once at the
 * top of the authenticated tree (inside the ProtectedRoute layer or
 * the dashboard shell).
 */
export function useDemoLeaseHeartbeat() {
  const isDemo = useAuthStore((s) => s.runtimeConfig?.demo_mode);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  useEffect(() => {
    if (!isDemo || !isAuthenticated) return;

    // Periodic heartbeat. Keep below the backend's lease TTL with
    // headroom for one missed cycle.
    const interval = window.setInterval(() => {
      demoApi.heartbeat().catch((err) => {
        // 404 means the lease expired — let the next user action
        // surface that as a re-auth prompt; we don't force-redirect
        // from here. Other errors are network noise.
        if (err?.response?.status !== 404) {
          logger.debug('demo heartbeat failed (non-404):', err?.message ?? err);
        }
      });
    }, 60_000);

    // Release on unload via sendBeacon — survives the page lifecycle
    // event that would cancel a regular fetch. Backend route accepts
    // empty body.
    const handleUnload = () => {
      try {
        navigator.sendBeacon('/api/demo/end');
      } catch {
        // older browsers without sendBeacon — ignore; the lease will
        // TTL out within 5 min anyway.
      }
    };
    window.addEventListener('beforeunload', handleUnload);

    return () => {
      window.clearInterval(interval);
      window.removeEventListener('beforeunload', handleUnload);
    };
  }, [isDemo, isAuthenticated]);
}
