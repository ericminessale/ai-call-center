import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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

// =============================================================================
// Wrap-up panel (Tier 2a) — shown for completed calls.
//
// Three sub-sections:
//   1. Disposition picker — required-ish; the "what was the outcome" tag.
//   2. Agent notes — free-text wrap-up, debounced autosave on blur / pause.
//   3. AI context — key/value display of what the AI captured during triage.
//
// The AI summary (interaction.summary) and sentiment arc are rendered
// elsewhere in CallDetailTab, so we deliberately don't duplicate them here.
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

  // Track whether the panel ever auto-saved — used to suppress the chip on
  // first render when the parent has prefilled values from the interaction.
  const initialNotes = useRef(interaction.agentNotes ?? '');
  const initialDisposition = useRef(interaction.dispositionCode ?? '');
  const notesDebounce = useRef<number | null>(null);

  // Re-sync when the parent swaps interactions (e.g. the user clicks a
  // different call). Without this we'd carry stale state across rows.
  useEffect(() => {
    setDispositionCode(interaction.dispositionCode ?? '');
    setNotes(interaction.agentNotes ?? '');
    setSaveState('idle');
    setErrorMsg(null);
    initialNotes.current = interaction.agentNotes ?? '';
    initialDisposition.current = interaction.dispositionCode ?? '';
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
        // Bubble up the new values so the surrounding view doesn't go stale.
        onUpdate?.({
          dispositionCode: res.data.call.disposition_code,
          agentNotes: res.data.call.agent_notes,
          wrappedUpAt: res.data.call.wrapped_up_at,
        });
        // Reset the "Saved" chip after a short pause so it doesn't loiter.
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
    saveWrapUp({ disposition_code: code || null });
  };

  const handleNotesChange = (value: string) => {
    setNotes(value);
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

  const aiContextEntries = useMemo(() => {
    const ctx = interaction.aiContext || {};
    return Object.entries(ctx).filter(
      ([, v]) =>
        v !== null &&
        v !== undefined &&
        v !== '' &&
        v !== 'unknown' &&
        v !== 'not specified'
    );
  }, [interaction.aiContext]);

  if (!isEligible) return null;

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-900/50 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 bg-gray-800/60 border-b border-gray-700">
        <div className="flex items-center gap-2 text-sm font-semibold text-white">
          <ClipboardList className="w-4 h-4 text-blue-400" />
          Wrap-up
          {interaction.wrappedUpAt && (
            <span className="text-[10px] uppercase tracking-wider text-gray-500 font-mono">
              · saved
            </span>
          )}
        </div>
        <SaveStatusChip state={saveState} error={errorMsg} />
      </div>

      <div className="p-4 space-y-4">
        {/* Disposition */}
        <div>
          <label className="block text-xs uppercase tracking-wider text-gray-500 mb-1.5">
            Disposition
          </label>
          {dispositions.length === 0 ? (
            <div className="text-xs text-gray-500 flex items-center gap-1.5">
              <Loader2 className="w-3 h-3 animate-spin" /> Loading…
            </div>
          ) : (
            <>
              <select
                value={dispositionCode}
                onChange={(e) => handleDispositionChange(e.target.value)}
                className="w-full px-3 py-2 text-sm rounded bg-gray-800 border border-gray-700 text-white focus:outline-none focus:ring-1 focus:ring-blue-400/40"
              >
                <option value="">— Select an outcome —</option>
                {dispositions.map((d) => (
                  <option key={d.code} value={d.code}>
                    {d.label}
                  </option>
                ))}
              </select>
              {dispositionCode && (
                <p className="mt-1 text-[11px] text-gray-500">
                  {dispositions.find((d) => d.code === dispositionCode)?.description}
                </p>
              )}
            </>
          )}
        </div>

        {/* Agent notes */}
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-xs uppercase tracking-wider text-gray-500">Notes</label>
            <span className="text-[10px] text-gray-600 font-mono">
              {notes.length}/{NOTES_MAX}
            </span>
          </div>
          <textarea
            value={notes}
            onChange={(e) => handleNotesChange(e.target.value.slice(0, NOTES_MAX))}
            onBlur={handleNotesBlur}
            placeholder="What happened? Add anything the next agent or this contact's record should know."
            rows={4}
            className="w-full px-3 py-2 text-sm rounded bg-gray-800 border border-gray-700 text-white placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-400/40 resize-none"
          />
        </div>

        {/* AI-captured context */}
        {aiContextEntries.length > 0 && (
          <div>
            <label className="block text-xs uppercase tracking-wider text-gray-500 mb-1.5">
              Captured by AI
            </label>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
              {aiContextEntries.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="text-gray-500 capitalize truncate">
                    {key.replace(/_/g, ' ')}
                  </dt>
                  <dd className="text-gray-300 truncate" title={String(value)}>
                    {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </div>
    </div>
  );
}

function SaveStatusChip({ state, error }: { state: SaveState; error: string | null }) {
  if (state === 'saving') {
    return (
      <span className="flex items-center gap-1 text-[11px] text-gray-400">
        <Loader2 className="w-3 h-3 animate-spin" />
        Saving…
      </span>
    );
  }
  if (state === 'saved') {
    return (
      <span className="flex items-center gap-1 text-[11px] text-green-400">
        <CheckCircle2 className="w-3 h-3" />
        Saved
      </span>
    );
  }
  if (state === 'error') {
    return (
      <span
        className="flex items-center gap-1 text-[11px] text-red-400"
        title={error || 'Failed to save'}
      >
        <AlertCircle className="w-3 h-3" />
        Couldn&apos;t save
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1 text-[11px] text-gray-600">
      <Save className="w-3 h-3" />
      Auto-saves
    </span>
  );
}
