import { useEffect, useRef } from 'react';
import toast from 'react-hot-toast';
import { demoApi } from '../services/api';
import { useSocketContext } from '../contexts/SocketContext';
import { useAuthStore } from '../stores/authStore';
import { logger } from '../lib/logger';

/**
 * Keep-alive for the visitor's demo workspace + WebRTC seat.
 *
 * While a demo-mode visitor is signed in, the seat lease TTL decays
 * unless we ping ``/api/demo/heartbeat`` regularly. We send a heartbeat
 * every 60s — well under the 300s default TTL, comfortable for a
 * network blip. Each beat also refreshes the workspace's expiry in the
 * auth store for the banner's lifetime chip.
 *
 * There is deliberately NO eager release on ``beforeunload``: that
 * event also fires on page reload, and releasing there bumps the
 * workspace epoch server-side, killing the JWT that survives in
 * localStorage — every F5 stranded the visitor. Instead the seat simply
 * TTLs out (~5 min) after the tab closes, and a reload keeps the same
 * workspace because ``checkAuth`` resumes via the still-valid session
 * cookie (see authStore.checkAuth).
 *
 * If the workspace expires while the tab is open (laptop sleep, long
 * network outage), the heartbeat's 404 triggers a one-shot restart:
 * same session cookie → fresh workspace → fresh tokens → reload. If
 * the install is at MAX_WORKSPACES we return the visitor to the
 * landing card honestly.
 *
 * Both behaviors are no-ops outside demo mode. Mount this once at the
 * top of the authenticated tree (inside the ProtectedRoute layer or
 * the dashboard shell).
 */
export function useDemoLeaseHeartbeat() {
  const isDemo = useAuthStore((s) => s.runtimeConfig?.demo_mode);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  // Only workspace-bound VISITORS have a demo session to keep alive. On a
  // hosted install a platform operator (workspace_id null) is signed in via
  // the real login — running the heartbeat for them would 404 (no demo
  // cookie), trigger recoverLease → /api/demo/start, and silently PROVISION
  // A VISITOR WORKSPACE, demoting the operator. It also must not react to
  // the demo:reset broadcast from a workspace the operator is merely
  // watching. Gate the whole hook on being a visitor.
  const isVisitor = useAuthStore((s) => s.user != null && s.user.workspace_id != null);
  const { socket } = useSocketContext();
  const recoveringRef = useRef(false);

  useEffect(() => {
    if (!isDemo || !isAuthenticated || !isVisitor) return;

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
    // headroom for one missed cycle. Each beat also carries the
    // workspace's (possibly just-extended) expiry, which feeds the
    // banner's lifetime display.
    const interval = window.setInterval(() => {
      demoApi
        .heartbeat()
        .then((res) => {
          if (res.data?.workspace) {
            useAuthStore.getState().setWorkspace(res.data.workspace);
          }
        })
        .catch((err) => {
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
  }, [isDemo, isAuthenticated, isVisitor]);

  // Listen for the workspace-reset broadcast. The reaper wipes this
  // visitor's workspace and emits demo:reset to its room, so we show a
  // polite toast and reload to re-land on the demo landing card. Reload
  // (rather than soft re-route) is intentional — clears any stale
  // in-memory state too (active call mocks, AI context, etc.). Gated on
  // isVisitor so a platform operator WATCHING a workspace that gets reaped
  // isn't force-reloaded by that workspace's demo:reset (they receive it
  // only because watch_workspace joined them to the room).
  useEffect(() => {
    if (!isDemo || !socket || !isVisitor) return;
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
  }, [isDemo, socket, isVisitor]);
}
