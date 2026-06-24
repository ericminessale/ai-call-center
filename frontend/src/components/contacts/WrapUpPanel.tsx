import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ClipboardList,
  CheckCircle2,
  Loader2,
  AlertCircle,
  Save,
} from 'lucide-react';
import { callsApi } from '../../services/api';
import { Interaction } from '../../types/callcenter';
import { logger } from '../../lib/logger';
import { AI_GLYPH } from '../restraint';

// =============================================================================
// Wrap-up panel (Tier 2a) — the CRM "how did the call conclude" surface.
//   1. Disposition picker — the outcome tag.
//   2. Agent notes — free-text, debounced autosave on blur / pause.
//   3. AI provenance badge — when the wrap-up is still the AI-suggested default
//      (never saved by a human), a subtle "captured by AI" marker sits under
//      the notes. It disappears the moment a human edits either field.
//
// The AI summary (interaction.summary) and sentiment arc render elsewhere in
// CallDetailTab; per-call metadata (language, etc.) lives in the call Details
// box — wrap-up is deliberately just disposition + notes.
// =============================================================================

const WRAP_UP_STATUSES = new Set([
  'ended',
  'completed',
  // We also show wrap-up while the call is winding down so an agent can
  // start tagging while the conversation memory is fresh.
  'on_hold',
]);

interface WrapUpPanelProps {
  interaction: Interaction;
  onUpdate?: (patch: Partial<Interaction>) => void;
}

interface Disposition {
  code: string;
  label: string;
  description: string;
}

type SaveState = 'idle' | 'saving' | 'saved' | 'error';

const NOTES_AUTOSAVE_MS = 1200;
const NOTES_MAX = 5000;

export function WrapUpPanel({ interaction, onUpdate }: WrapUpPanelProps) {
  const isEligible = WRAP_UP_STATUSES.has(interaction.status);

  const [dispositions, setDispositions] = useState<Disposition[]>([]);
  const [dispositionCode, setDispositionCode] = useState<string>(interaction.dispositionCode ?? '');
  const [notes, setNotes] = useState<string>(interaction.agentNotes ?? '');
  const [saveState, setSaveState] = useState<SaveState>('idle');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  // Whether a human has edited the wrap-up this session — flips the AI
  // provenance badge off immediately, before the autosave round-trips.
  const [humanTouched, setHumanTouched] = useState(false);

  const initialNotes = useRef(interaction.agentNotes ?? '');
  const notesDebounce = useRef<number | null>(null);

  // Re-sync when the parent swaps interactions (e.g. the user clicks a
  // different call). Without this we'd carry stale state across rows.
  useEffect(() => {
    setDispositionCode(interaction.dispositionCode ?? '');
    setNotes(interaction.agentNotes ?? '');
    setSaveState('idle');
    setErrorMsg(null);
    setHumanTouched(false);
    initialNotes.current = interaction.agentNotes ?? '';
  }, [interaction.id]);

  // Pull the disposition list once. Cheap enough to lazy-load on mount.
  useEffect(() => {
    if (!isEligible) return;
    callsApi
      .listDispositions()
      .then((res) => setDispositions(res.data.dispositions))
      .catch((err) => logger.error('Failed to load dispositions', err));
  }, [isEligible]);

  const saveWrapUp = useCallback(
    async (patch: { disposition_code?: string | null; agent_notes?: string | null }) => {
      try {
        setSaveState('saving');
        setErrorMsg(null);
        const res = await callsApi.saveWrapUp(interaction.signalwireCallSid || interaction.id, patch);
        setSaveState('saved');
        onUpdate?.({
          dispositionCode: res.data.call.disposition_code,
          agentNotes: res.data.call.agent_notes,
          wrappedUpAt: res.data.call.wrapped_up_at,
          wrapUpSource: res.data.call.wrap_up_source,
        });
        window.setTimeout(() => {
          setSaveState((s) => (s === 'saved' ? 'idle' : s));
        }, 1800);
      } catch (err: any) {
        logger.error('Failed to save wrap-up', err);
        setSaveState('error');
        setErrorMsg(err?.response?.data?.error || 'Failed to save');
      }
    },
    [interaction.signalwireCallSid, interaction.id, onUpdate]
  );

  const handleDispositionChange = (code: string) => {
    setDispositionCode(code);
    setHumanTouched(true);
    saveWrapUp({ disposition_code: code || null });
  };

  const handleNotesChange = (value: string) => {
    setNotes(value);
    setHumanTouched(true);
    if (notesDebounce.current) window.clearTimeout(notesDebounce.current);
    notesDebounce.current = window.setTimeout(() => {
      saveWrapUp({ agent_notes: value || null });
    }, NOTES_AUTOSAVE_MS);
  };

  // Flush pending autosave on blur — gives the user a deterministic save point.
  const handleNotesBlur = () => {
    if (notesDebounce.current) {
      window.clearTimeout(notesDebounce.current);
      notesDebounce.current = null;
    }
    if (notes !== initialNotes.current) {
      saveWrapUp({ agent_notes: notes || null });
      initialNotes.current = notes;
    }
  };

  // AI-captured = the wrap-up still carries the AI's auto-filled values and no
  // human has claimed it. wrapUpSource is stamped explicitly at the source —
  // 'ai' by the post-prompt prefill, 'agent' the moment a human saves an edit —
  // so this is a fact, not an inference. humanTouched flips it off instantly
  // in-session, before the save round-trips.
  const aiCaptured = !humanTouched && interaction.wrapUpSource === 'ai';

  if (!isEligible) return null;

  return (
    <div className="rounded-lg border border-rule bg-canvas-raised overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-rule">
        <div className="flex items-center gap-2 text-[13px] font-semibold text-ink">
          <ClipboardList className="w-4 h-4 text-ink-muted" />
          Wrap-up
          {interaction.wrappedUpAt && (
            <span className="text-[10px] uppercase tracking-wider text-ink-dim mono">· saved</span>
          )}
        </div>
        <SaveStatusChip state={saveState} error={errorMsg} />
      </div>

      <div className="p-4 space-y-4">
        {/* Disposition */}
        <div>
          <label className="block text-[11px] font-medium text-ink-dim mb-1.5">Disposition</label>
          {dispositions.length === 0 ? (
            <div className="text-[11px] text-ink-dim flex items-center gap-1.5">
              <Loader2 className="w-3 h-3 animate-spin" /> Loading…
            </div>
          ) : (
            <>
              <select
                value={dispositionCode}
                onChange={(e) => handleDispositionChange(e.target.value)}
                className="input"
              >
                <option value="">— Select an outcome —</option>
                {dispositions.map((d) => (
                  <option key={d.code} value={d.code}>
                    {d.label}
                  </option>
                ))}
              </select>
              {dispositionCode && (
                <p className="mt-1 text-[11px] text-ink-dim">
                  {dispositions.find((d) => d.code === dispositionCode)?.description}
                </p>
              )}
            </>
          )}
        </div>

        {/* Agent notes */}
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-[11px] font-medium text-ink-dim">Notes</label>
            <span className="text-[10px] text-ink-dim mono">{notes.length}/{NOTES_MAX}</span>
          </div>
          <textarea
            value={notes}
            onChange={(e) => handleNotesChange(e.target.value.slice(0, NOTES_MAX))}
            onBlur={handleNotesBlur}
            placeholder="How did the call conclude? Note anything the next agent or this contact's record should know."
            rows={4}
            className="input resize-none"
          />
          {/* AI provenance — subtle, disappears once a human edits. */}
          {aiCaptured && (
            <div
              className="mt-1.5 flex items-center gap-1.5 text-[11px]"
              title="This wrap-up was drafted by the AI. Editing the disposition or notes makes it yours."
            >
              <span aria-hidden className="text-ai">{AI_GLYPH}</span>
              <span className="text-ink-dim">Captured by AI — edit to take over</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SaveStatusChip({ state, error }: { state: SaveState; error: string | null }) {
  if (state === 'saving') {
    return (
      <span className="flex items-center gap-1 text-[11px] text-ink-dim">
        <Loader2 className="w-3 h-3 animate-spin" />
        Saving…
      </span>
    );
  }
  if (state === 'saved') {
    return (
      <span className="flex items-center gap-1 text-[11px] text-status-success">
        <CheckCircle2 className="w-3 h-3" />
        Saved
      </span>
    );
  }
  if (state === 'error') {
    return (
      <span className="flex items-center gap-1 text-[11px] text-status-error" title={error || 'Failed to save'}>
        <AlertCircle className="w-3 h-3" />
        Couldn&apos;t save
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1 text-[11px] text-ink-dim">
      <Save className="w-3 h-3" />
      Auto-saves
    </span>
  );
}
