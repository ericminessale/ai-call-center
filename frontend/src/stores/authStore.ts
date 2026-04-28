import { create } from 'zustand';
import { User } from '../types';
import { authApi, demoApi, runtimeApi, RuntimeConfig } from '../services/api';
import websocket from '../services/websocket';

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
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => void;
  checkAuth: () => Promise<void>;
  fetchRuntimeConfig: () => Promise<void>;
  startDemoSession: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: false,
  isLoading: false,
  isCheckingAuth: true, // Start as true since we'll check on mount
  error: null,
  runtimeConfig: null,

  login: async (email, password) => {
    set({ isLoading: true, error: null });
    try {
      const response = await authApi.login(email, password);
      const { access_token, refresh_token, user } = response.data;

      localStorage.setItem('access_token', access_token);
      localStorage.setItem('refresh_token', refresh_token);

      websocket.connect(access_token);

      set({ user, isAuthenticated: true, isLoading: false });
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
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    websocket.disconnect();
    set({ user: null, isAuthenticated: false });
  },

  checkAuth: async () => {
    set({ isCheckingAuth: true });
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
      set({ user: null, isAuthenticated: false, isCheckingAuth: false });
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
      const { access_token, refresh_token, user } = response.data;
      localStorage.setItem('access_token', access_token);
      localStorage.setItem('refresh_token', refresh_token);
      websocket.connect(access_token);
      set({ user, isAuthenticated: true, isLoading: false });
    } catch (error: any) {
      set({
        error: error.response?.data?.error || 'Could not start demo session',
        isLoading: false,
      });
      throw error;
    }
  },
}));