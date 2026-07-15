import { create } from 'zustand';
import { demoApi } from '../services/api';

/**
 * Shared hosted-demo phone-verification state.
 *
 * Verification gates telephony (a workspace accepts inbound calls only from,
 * and the AI can call back only, the visitor's verified number). Multiple
 * surfaces need to know the state — the verification card, the demo banner
 * badge, and the "locked until verified" hints on the telephony views — so
 * it lives in one store instead of being re-fetched per component.
 *
 * Hydrated + kept live by useDemoVerification() (mounted once in the demo
 * shell); everything else reads this store.
 */
interface VerifyState {
  verified: boolean;
  code: string | null;
  maskedNumber: string | null;
  hydrated: boolean;   // false until the first status fetch resolves
  requesting: boolean; // a pairing-code request is in flight
  hydrate: () => Promise<void>;
  requestCode: () => Promise<string | null>;
  markVerified: (maskedNumber: string | null) => void;
  reset: () => void;
}

export const useVerifyStore = create<VerifyState>((set, get) => ({
  verified: false,
  code: null,
  maskedNumber: null,
  hydrated: false,
  requesting: false,

  hydrate: async () => {
    try {
      const r = await demoApi.verifyStatus();
      set({
        verified: r.data.verified,
        maskedNumber: r.data.masked_number,
        // Keep any locally-issued code if the server didn't echo one back.
        code: r.data.verified ? null : (r.data.code ?? get().code),
        hydrated: true,
      });
    } catch {
      set({ hydrated: true });
    }
  },

  requestCode: async () => {
    if (get().requesting) return get().code;
    set({ requesting: true });
    try {
      const r = await demoApi.pairingCode();
      set({ code: r.data.code, requesting: false });
      return r.data.code;
    } catch {
      set({ requesting: false });
      throw new Error('pairing-code request failed');
    }
  },

  markVerified: (maskedNumber) =>
    set({ verified: true, maskedNumber, code: null }),

  reset: () =>
    set({ verified: false, code: null, maskedNumber: null, hydrated: false, requesting: false }),
}));
