import { create } from 'zustand';
import { DemoWorkspace, User } from '../types';
import { authApi, demoApi, runtimeApi, RuntimeConfig } from '../services/api';
import websocket from '../services/websocket';

// localStorage key marking "this browser holds a hosted-demo session".
// Set by startDemoSession, consumed by checkAuth (restore path) and
// logout (eager lease release). Deliberately NOT in the zustand state —
// it must survive reloads, which is the whole point.
const DEMO_SESSION_FLAG = 'demo_session';

interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  isCheckingAuth: boolean;
  error: string | null;
  // Runtime config from the backend — tells the UI whether this is the
  // hosted demo (in which case we render a landing card instead of the
  // login form). null until fetchRuntimeConfig() resolves on app boot.
  runtimeConfig: RuntimeConfig | null;
  // Hosted demo only: the visitor's workspace (name/status/expires_at).
  // Set by /demo/start, refreshed by the 60s heartbeat so the banner's
  // lifetime display stays honest. null for real logins.
  workspace: DemoWorkspace | null;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => void;
  checkAuth: () => Promise<void>;
  fetchRuntimeConfig: () => Promise<void>;
  startDemoSession: () => Promise<void>;
  setWorkspace: (workspace: DemoWorkspace | null) => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: false,
  isLoading: false,
  isCheckingAuth: true, // Start as true since we'll check on mount
  error: null,
  runtimeConfig: null,
  workspace: null,

  login: async (email, password) => {
    set({ isLoading: true, error: null });
    try {
      const response = await authApi.login(email, password);
      const { access_token, refresh_token, user } = response.data;

      // A real password login supersedes any demo session this browser
      // held (e.g. a platform operator who tried the demo first). Without
      // this, checkAuth's demo-flag path silently swaps the operator back
      // to the visitor workspace on the next reload.
      localStorage.removeItem(DEMO_SESSION_FLAG);

      localStorage.setItem('access_token', access_token);
      localStorage.setItem('refresh_token', refresh_token);

      websocket.connect(access_token);

      set({ user, workspace: null, isAuthenticated: true, isLoading: false });
    } catch (error: any) {
      set({
        error: error.response?.data?.error || 'Login failed',
        isLoading: false
      });
      throw error;
    }
  },

  register: async (email, password) => {
    set({ isLoading: true, error: null });
    try {
      const response = await authApi.register(email, password);
      const { access_token, refresh_token, user } = response.data;

      localStorage.setItem('access_token', access_token);
      localStorage.setItem('refresh_token', refresh_token);

      websocket.connect(access_token);

      set({ user, isAuthenticated: true, isLoading: false });
    } catch (error: any) {
      set({
        error: error.response?.data?.error || 'Registration failed',
        isLoading: false
      });
      throw error;
    }
  },

  logout: () => {
    // Demo sessions release their persona lease eagerly so it returns
    // to the pool for the next visitor instead of waiting out the TTL.
    if (localStorage.getItem(DEMO_SESSION_FLAG) === '1') {
      demoApi.end().catch(() => {});
      localStorage.removeItem(DEMO_SESSION_FLAG);
    }
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    websocket.disconnect();
    set({ user: null, workspace: null, isAuthenticated: false });
  },

  checkAuth: async () => {
    set({ isCheckingAuth: true });

    // Demo sessions don't restore via /auth/me — persona JWTs are bound
    // to the lease epoch and may be stale after a reload. Instead we
    // re-call /demo/start: the HttpOnly session cookie survives the
    // reload, so the backend refreshes the existing lease and returns
    // the SAME persona with fresh tokens (or leases a new one if the
    // old lease lapsed). Seamless F5 for demo visitors.
    if (localStorage.getItem(DEMO_SESSION_FLAG) === '1') {
      try {
        const response = await demoApi.start();
        const { access_token, refresh_token, user, workspace } = response.data;
        localStorage.setItem('access_token', access_token);
        localStorage.setItem('refresh_token', refresh_token);
        websocket.connect(access_token);
        set({ user, workspace: workspace ?? null, isAuthenticated: true, isCheckingAuth: false });
        return;
      } catch (error: any) {
        // Only drop the flag on definitive signals: demo turned off
        // (404) or pool full (503 — our lease is gone anyway). A 429
        // (rate-limit) or network blip is transient — keep the flag so
        // the next reload retries /demo/start, and keep logout's eager
        // lease release working. Either way fall through to the token
        // path, which lands unauthenticated if the persona token is
        // stale.
        const status = error?.response?.status;
        if (status === 404 || status === 503) {
          localStorage.removeItem(DEMO_SESSION_FLAG);
        }
      }
    }

    const token = localStorage.getItem('access_token');

    if (!token) {
      set({ isCheckingAuth: false, isAuthenticated: false });
      return;
    }

    try {
      // Verify token and get user data
      const response = await authApi.me();
      const { user } = response.data;

      websocket.connect(token);
      set({ user, isAuthenticated: true, isCheckingAuth: false });
    } catch (error) {
      // Token is invalid or expired
      localStorage.removeItem('access_token');
      localStorage.removeItem('refresh_token');
      set({ user: null, workspace: null, isAuthenticated: false, isCheckingAuth: false });
    }
  },

  fetchRuntimeConfig: async () => {
    // Public endpoint — no auth needed, no error state to expose.
    // Failures (backend down at boot) leave runtimeConfig=null and the
    // app falls through to its production-shape default.
    try {
      const response = await runtimeApi.get();
      set({ runtimeConfig: response.data });
    } catch {
      // Treat as "not in demo mode" — safest default.
      set({ runtimeConfig: { demo_mode: false, demo_phone_numbers: [] } });
    }
  },

  startDemoSession: async () => {
    // Hits the demo-only no-auth endpoint and reuses the same login
    // wiring that authApi.login does. Any failure (404 in production,
    // 503 if pool not seeded yet) bubbles up as a thrown error.
    set({ isLoading: true, error: null });
    try {
      const response = await demoApi.start();
      const { access_token, refresh_token, user, workspace } = response.data;
      localStorage.setItem('access_token', access_token);
      localStorage.setItem('refresh_token', refresh_token);
      // Marks this browser as holding a demo session so checkAuth
      // restores it via /demo/start (lease-aware) instead of /auth/me.
      localStorage.setItem(DEMO_SESSION_FLAG, '1');
      websocket.connect(access_token);
      set({ user, workspace: workspace ?? null, isAuthenticated: true, isLoading: false });
    } catch (error: any) {
      set({
        error: error.response?.data?.error || 'Could not start demo session',
        isLoading: false,
      });
      throw error;
    }
  },

  setWorkspace: (workspace) => set({ workspace }),
}));