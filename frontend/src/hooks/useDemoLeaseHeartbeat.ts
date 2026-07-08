import { useEffect, useRef } from 'react';
import toast from 'react-hot-toast';
import { demoApi } from '../services/api';
import { useSocketContext } from '../contexts/SocketContext';
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
 * There is deliberately NO eager release on ``beforeunload``: that
 * event also fires on page reload, and releasing there bumps the
 * persona epoch server-side, killing the JWT that survives in
 * localStorage — every F5 stranded the visitor. Instead the lease
 * simply TTLs out (~5 min) after the tab closes, and a reload keeps
 * the same persona because ``checkAuth`` re-leases via the still-valid
 * session cookie (see authStore.checkAuth).
 *
 * If the lease expires while the tab is open (laptop sleep, long
 * network outage), the heartbeat's 404 triggers a one-shot re-lease:
 * same session cookie → usually the same persona → fresh tokens →
 * reload. If the pool is full we return the visitor to the landing
 * card honestly.
 *
 * Both behaviors are no-ops outside demo mode. Mount this once at the
 * top of the authenticated tree (inside the ProtectedRoute layer or
 * the dashboard shell).
 */
export function useDemoLeaseHeartbeat() {
  const isDemo = useAuthStore((s) => s.runtimeConfig?.demo_mode);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const { socket } = useSocketContext();
  const recoveringRef = useRef(false);

  useEffect(() => {
    if (!isDemo || !isAuthenticated) return;

    const recoverLease = async () => {
      if (recoveringRef.current) return;
      recoveringRef.current = true;
      try {
        const res = await demoApi.start();
        localStorage.setItem('access_token', res.data.access_token);
        localStorage.setItem('refresh_token', res.data.refresh_token);
        toast('Demo session refreshed — reloading…', { icon: '↻', duration: 3000 });
        // Reload rather than hot-swapping tokens: the websocket and any
        // in-flight state were authenticated as the old lease; a clean
        // boot re-establishes everything against the new one.
        window.setTimeout(() => window.location.reload(), 1200);
      } catch (err) {
        logger.debug('demo lease recovery failed:', err);
        toast.error('Your demo session expired. Returning to the start page…');
        window.setTimeout(() => {
          useAuthStore.getState().logout();
          window.location.assign('/login');
        }, 1200);
      }
    };

    // Periodic heartbeat. Keep below the backend's lease TTL with
    // headroom for one missed cycle.
    const interval = window.setInterval(() => {
      demoApi.heartbeat().catch((err) => {
        if (err?.response?.status === 404) {
          // Lease expired while the tab was open — try to re-lease.
          void recoverLease();
        } else {
          logger.debug('demo heartbeat failed (non-404):', err?.message ?? err);
        }
      });
    }, 60_000);

    return () => {
      window.clearInterval(interval);
    };
  }, [isDemo, isAuthenticated]);

  // Listen for the nightly reset broadcast. The backend wipes mutable
  // state including this visitor's lease, so we show a polite toast
  // and reload to re-land on the demo landing card. Reload (rather
  // than soft re-route) is intentional — clears any stale in-memory
  // state too (active call mocks, AI context, etc.).
  useEffect(() => {
    if (!isDemo || !socket) return;
    const onReset = (payload: { message?: string } = {}) => {
      const msg = payload.message ?? 'Demo refreshing — reloading…';
      toast(msg, { icon: '↻', duration: 4000 });
      // Small delay so the toast renders before the reload kills it.
      window.setTimeout(() => window.location.reload(), 1500);
    };
    socket.on('demo:reset', onReset);
    return () => {
      socket.off('demo:reset', onReset);
    };
  }, [isDemo, socket]);
}
