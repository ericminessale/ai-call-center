/**
 * CallControlPanel — participant controls for a call the user is currently on.
 *
 * Renders self actions (mute lives on the header) and participant actions
 * (hold, record, TTS, DTMF, translate, request-backup, escalate). Demonstrates
 * SignalWire's real-time call control: calls are stateful objects you command
 * by UUID.
 *
 * Observer actions (Listen / Whisper / Barge) deliberately do NOT live here —
 * they apply to calls you are NOT on. See ObserverControls and the architecture
 * discussion in the call-controls refactor.
 */

import React, { useState, useCallback, useEffect } from 'react';
import {
  Pause,
  Play,
  Circle,
  Volume2,
  Hash,
  Send,
  Users,
  ShieldAlert,
  ChevronDown,
  ChevronUp,
  StopCircle,
  Languages,
} from 'lucide-react';
import { callControlApi } from '../../services/api';

// Same BCP-47 menu as the admin user editor so caller/agent vocabularies match.
const TRANSLATE_LANGUAGES: { code: string; label: string }[] = [
  { code: 'en-US', label: 'English (US)' },
  { code: 'es-ES', label: 'Spanish' },
  { code: 'fr-FR', label: 'French' },
  { code: 'de-DE', label: 'German' },
  { code: 'it-IT', label: 'Italian' },
  { code: 'pt-BR', label: 'Portuguese (Brazil)' },
  { code: 'ja-JP', label: 'Japanese' },
  { code: 'zh-CN', label: 'Chinese (Mandarin)' },
  { code: 'ko-KR', label: 'Korean' },
  { code: 'ar-SA', label: 'Arabic' },
];

interface CallControlPanelProps {
  callId: number | string;
  callSid: string;
  isAICall: boolean;
  isHumanCall: boolean;
  isInConference: boolean;
  isOnHold: boolean;
  isRecording: boolean;
  userRole?: string;
  onHoldChange?: (held: boolean) => void;
  onRecordingChange?: (recording: boolean) => void;
  onBackupRequested?: () => void;
  onEscalateRequested?: () => void;
}

export default function CallControlPanel({
  callId,
  callSid,
  isAICall,
  isHumanCall,
  isInConference,
  isOnHold,
  isRecording,
  userRole = 'agent',
  onHoldChange,
  onRecordingChange,
  onBackupRequested,
  onEscalateRequested,
}: CallControlPanelProps) {
  const [showDtmfPad, setShowDtmfPad] = useState(false);
  const [showTtsInput, setShowTtsInput] = useState(false);
  const [ttsText, setTtsText] = useState('');
  const [isLoading, setIsLoading] = useState<string | null>(null); // track which action is loading
  const [expanded, setExpanded] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showTranslatePicker, setShowTranslatePicker] = useState(false);
  const [translateActive, setTranslateActive] = useState(false);
  const [translateFromLang, setTranslateFromLang] = useState<string>('es-ES');
  const [translateToLang, setTranslateToLang] = useState<string>('en-US');

  const isSupervisor = userRole === 'supervisor' || userRole === 'admin';

  const clearError = () => setError(null);

  const handleHold = useCallback(async () => {
    setIsLoading('hold');
    clearError();
    try {
      if (isOnHold) {
        await callControlApi.unhold(callId);
        onHoldChange?.(false);
      } else {
        await callControlApi.hold(callId);
        onHoldChange?.(true);
      }
    } catch (e: any) {
      setError(e.response?.data?.error || 'Failed to hold/unhold call');
    } finally {
      setIsLoading(null);
    }
  }, [callId, isOnHold, onHoldChange]);

  const handleRecord = useCallback(async () => {
    setIsLoading('record');
    clearError();
    try {
      if (isRecording) {
        await callControlApi.stopRecording(callId);
        onRecordingChange?.(false);
      } else {
        await callControlApi.startRecording(callId);
        onRecordingChange?.(true);
      }
    } catch (e: any) {
      setError(e.response?.data?.error || 'Failed to toggle recording');
    } finally {
      setIsLoading(null);
    }
  }, [callId, isRecording, onRecordingChange]);

  const handlePlayTts = useCallback(async () => {
    if (!ttsText.trim()) return;
    setIsLoading('tts');
    clearError();
    try {
      await callControlApi.playTts(callId, ttsText.trim());
      setTtsText('');
      setShowTtsInput(false);
    } catch (e: any) {
      setError(e.response?.data?.error || 'Failed to play TTS');
    } finally {
      setIsLoading(null);
    }
  }, [callId, ttsText]);

  const handleDtmf = useCallback(async (digit: string) => {
    clearError();
    try {
      await callControlApi.sendDtmf(callId, digit);
    } catch (e: any) {
      setError(e.response?.data?.error || 'Failed to send DTMF');
    }
  }, [callId]);

  const handleRequestBackup = useCallback(async () => {
    setIsLoading('backup');
    clearError();
    try {
      await callControlApi.requestBackup(callId);
      onBackupRequested?.();
    } catch (e: any) {
      setError(e.response?.data?.error || 'No agents available for backup');
    } finally {
      setIsLoading(null);
    }
  }, [callId, onBackupRequested]);

  // Hydrate translate state from backend so the panel reflects auto-started
  // translations and survives reloads / takeovers.
  useEffect(() => {
    let cancelled = false;
    callControlApi
      .getTranslateStatus(callId)
      .then((res) => {
        if (cancelled) return;
        setTranslateActive(res.data.active);
        if (res.data.from_lang) setTranslateFromLang(res.data.from_lang);
        if (res.data.to_lang) setTranslateToLang(res.data.to_lang);
        // Pre-fill from caller_language if no active session
        if (!res.data.active && res.data.caller_language) {
          setTranslateFromLang(res.data.caller_language);
        }
      })
      .catch(() => {
        // Silent — endpoint may 404 for non-conference calls; just leave defaults.
      });
    return () => {
      cancelled = true;
    };
  }, [callId]);

  const handleStartTranslate = useCallback(async () => {
    if (translateFromLang === translateToLang) {
      setError('Pick two different languages to translate between');
      return;
    }
    setIsLoading('translate');
    clearError();
    try {
      await callControlApi.startTranslate(callId, translateFromLang, translateToLang);
      setTranslateActive(true);
      setShowTranslatePicker(false);
    } catch (e: any) {
      setError(e.response?.data?.error || 'Failed to start translation');
    } finally {
      setIsLoading(null);
    }
  }, [callId, translateFromLang, translateToLang]);

  const handleStopTranslate = useCallback(async () => {
    setIsLoading('translate');
    clearError();
    try {
      await callControlApi.stopTranslate(callId);
      setTranslateActive(false);
    } catch (e: any) {
      setError(e.response?.data?.error || 'Failed to stop translation');
    } finally {
      setIsLoading(null);
    }
  }, [callId]);

  const handleToggleTranslate = useCallback(() => {
    if (translateActive) {
      handleStopTranslate();
    } else {
      // Open picker to choose languages first
      setShowTranslatePicker((s) => !s);
      setShowTtsInput(false);
      setShowDtmfPad(false);
    }
  }, [translateActive, handleStopTranslate]);

  const handleEscalate = useCallback(async () => {
    setIsLoading('escalate');
    clearError();
    try {
      await callControlApi.escalate(callId, true); // whisper mode by default
      onEscalateRequested?.();
    } catch (e: any) {
      setError(e.response?.data?.error || 'No supervisors available');
    } finally {
      setIsLoading(null);
    }
  }, [callId, onEscalateRequested]);

  const dtmfButtons = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#'];

  return (
    <div className="mt-3">
      {/* Toggle header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-300 transition-colors mb-2"
      >
        {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <span className="uppercase tracking-wider font-medium">Call Controls</span>
      </button>

      {expanded && (
        <div className="space-y-2">
          {/* Primary control buttons */}
          <div className="flex flex-wrap items-center gap-2">
            {/* Hold/Resume - only for human agent calls */}
            {isHumanCall && (
              <button
                onClick={handleHold}
                disabled={isLoading === 'hold'}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  isOnHold
                    ? 'bg-yellow-600 hover:bg-yellow-700 text-white'
                    : 'bg-gray-700 hover:bg-gray-600 text-gray-200'
                } disabled:opacity-50`}
              >
                {isOnHold ? <Play className="w-3.5 h-3.5" /> : <Pause className="w-3.5 h-3.5" />}
                {isLoading === 'hold' ? '...' : isOnHold ? 'Resume' : 'Hold'}
              </button>
            )}

            {/* Record — only for human agent calls (AI calls auto-record via SWML).
                Button state is hydrated from GET /record/status on mount so it
                accurately reflects whether a manual recording session is live. */}
            {isHumanCall && (
              <button
                onClick={handleRecord}
                disabled={isLoading === 'record'}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  isRecording
                    ? 'bg-red-600 hover:bg-red-700 text-white'
                    : 'bg-gray-700 hover:bg-gray-600 text-gray-200'
                } disabled:opacity-50`}
                title={
                  isRecording
                    ? 'Stop the manual recording session'
                    : 'Start recording this call on demand. AI calls already record by default via SWML.'
                }
              >
                {isRecording ? (
                  <>
                    <StopCircle className="w-3.5 h-3.5" />
                    <span className="flex items-center gap-1">
                      Stop
                      <span className="w-2 h-2 rounded-full bg-red-400 animate-pulse" />
                    </span>
                  </>
                ) : (
                  <>
                    <Circle className="w-3.5 h-3.5" />
                    {isLoading === 'record' ? '...' : 'Record'}
                  </>
                )}
              </button>
            )}

            {/* TTS toggle - only for human agent calls (AI verb owns audio pipeline) */}
            {isHumanCall && (
              <button
                onClick={() => { setShowTtsInput(!showTtsInput); setShowDtmfPad(false); }}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  showTtsInput
                    ? 'bg-blue-600 hover:bg-blue-700 text-white'
                    : 'bg-gray-700 hover:bg-gray-600 text-gray-200'
                }`}
                title="Speak a synthesized message into the call. Useful for canned greetings or reading back data hands-free."
              >
                <Volume2 className="w-3.5 h-3.5" />
                TTS
              </button>
            )}

            {/* DTMF toggle - only for human agent calls */}
            {isHumanCall && (
              <button
                onClick={() => { setShowDtmfPad(!showDtmfPad); setShowTtsInput(false); }}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  showDtmfPad
                    ? 'bg-blue-600 hover:bg-blue-700 text-white'
                    : 'bg-gray-700 hover:bg-gray-600 text-gray-200'
                }`}
                title="Send touch-tone digits into the call. Use when the caller has been transferred to an external IVR (e.g. a bank) and needs navigation."
              >
                <Hash className="w-3.5 h-3.5" />
                DTMF
              </button>
            )}

            {/* Live Translate toggle - only for human agent calls */}
            {isHumanCall && (
              <button
                onClick={handleToggleTranslate}
                disabled={isLoading === 'translate'}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 ${
                  translateActive
                    ? 'bg-emerald-600 hover:bg-emerald-700 text-white ring-1 ring-emerald-400'
                    : showTranslatePicker
                      ? 'bg-blue-600 hover:bg-blue-700 text-white'
                      : 'bg-gray-700 hover:bg-gray-600 text-gray-200'
                }`}
                title={translateActive ? `Translating ${translateFromLang} ↔ ${translateToLang}` : 'Start live translation'}
              >
                <Languages className="w-3.5 h-3.5" />
                {isLoading === 'translate'
                  ? '...'
                  : translateActive
                    ? `${translateFromLang.split('-')[0]}↔${translateToLang.split('-')[0]}`
                    : 'Translate'}
              </button>
            )}

            {/* Separator */}
            <div className="w-px h-6 bg-gray-600 mx-1" />

            {/* Listen/Whisper/Barge intentionally omitted — those are observer
                actions for calls you are NOT on. See ObserverControls. */}

            {/* Request Backup (when in conference) */}
            {isInConference && isHumanCall && (
              <button
                onClick={handleRequestBackup}
                disabled={isLoading === 'backup'}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-700 hover:bg-amber-600 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
              >
                <Users className="w-3.5 h-3.5" />
                {isLoading === 'backup' ? '...' : 'Backup'}
              </button>
            )}

            {/* Escalate to Supervisor */}
            {isInConference && isHumanCall && !isSupervisor && (
              <button
                onClick={handleEscalate}
                disabled={isLoading === 'escalate'}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-orange-700 hover:bg-orange-600 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
              >
                <ShieldAlert className="w-3.5 h-3.5" />
                {isLoading === 'escalate' ? '...' : 'Escalate'}
              </button>
            )}
          </div>

          {/* TTS Input */}
          {showTtsInput && (
            <div className="flex items-center gap-2 p-2 bg-gray-800 rounded-lg border border-gray-700">
              <input
                type="text"
                value={ttsText}
                onChange={(e) => setTtsText(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handlePlayTts()}
                placeholder="Type a message to speak into the call..."
                className="flex-1 px-3 py-1.5 bg-gray-700 border border-gray-600 rounded text-sm text-white placeholder-gray-500 focus:outline-none focus:border-blue-500"
                autoFocus
              />
              <button
                onClick={handlePlayTts}
                disabled={!ttsText.trim() || isLoading === 'tts'}
                className="flex items-center gap-1 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded text-sm font-medium transition-colors disabled:opacity-50"
              >
                <Send className="w-3.5 h-3.5" />
                {isLoading === 'tts' ? '...' : 'Speak'}
              </button>
            </div>
          )}

          {/* Translate language picker (shown for initial start AND mid-call language change) */}
          {showTranslatePicker && (
            <div className="p-3 bg-gray-800 rounded-lg border border-gray-700 space-y-2">
              <div className="text-xs text-gray-400 uppercase tracking-wider font-medium mb-1">
                Live Translate
              </div>
              <div className="flex items-center gap-2">
                <div className="flex-1">
                  <label className="block text-[10px] text-gray-500 mb-1 uppercase">Caller speaks</label>
                  <select
                    value={translateFromLang}
                    onChange={(e) => setTranslateFromLang(e.target.value)}
                    className="w-full px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-xs text-white"
                  >
                    {TRANSLATE_LANGUAGES.map((l) => (
                      <option key={l.code} value={l.code}>
                        {l.label} ({l.code})
                      </option>
                    ))}
                  </select>
                </div>
                <span className="text-gray-500 text-lg mt-4">↔</span>
                <div className="flex-1">
                  <label className="block text-[10px] text-gray-500 mb-1 uppercase">You speak</label>
                  <select
                    value={translateToLang}
                    onChange={(e) => setTranslateToLang(e.target.value)}
                    className="w-full px-2 py-1.5 bg-gray-700 border border-gray-600 rounded text-xs text-white"
                  >
                    {TRANSLATE_LANGUAGES.map((l) => (
                      <option key={l.code} value={l.code}>
                        {l.label} ({l.code})
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="flex gap-2 pt-1">
                <button
                  onClick={handleStartTranslate}
                  disabled={isLoading === 'translate' || translateFromLang === translateToLang}
                  className="flex-1 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded text-xs font-medium transition-colors disabled:opacity-50"
                >
                  {isLoading === 'translate'
                    ? (translateActive ? 'Updating...' : 'Starting...')
                    : (translateActive ? 'Update languages' : 'Start translation')}
                </button>
                <button
                  onClick={() => setShowTranslatePicker(false)}
                  className="px-3 py-1.5 text-gray-400 hover:text-white text-xs"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {/* Active translation status with change-language button */}
          {translateActive && (
            <div className="flex items-center justify-between p-2 bg-emerald-900/30 border border-emerald-700/50 rounded text-xs">
              <span className="flex items-center gap-2 text-emerald-300">
                <Languages className="w-3.5 h-3.5" />
                Translating <span className="mono">{translateFromLang}</span> ↔ <span className="mono">{translateToLang}</span>
              </span>
              <button
                onClick={() => setShowTranslatePicker(true)}
                className="text-emerald-400 hover:text-emerald-200 underline"
              >
                Change languages
              </button>
            </div>
          )}

          {/* DTMF Pad */}
          {showDtmfPad && (
            <div className="p-2 bg-gray-800 rounded-lg border border-gray-700">
              <div className="grid grid-cols-3 gap-1.5 max-w-[180px]">
                {dtmfButtons.map((digit) => (
                  <button
                    key={digit}
                    onClick={() => handleDtmf(digit)}
                    className="w-14 h-10 bg-gray-700 hover:bg-gray-600 active:bg-gray-500 text-white font-mono text-lg rounded transition-colors"
                  >
                    {digit}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* AudioMonitor for tap-based listening lives in ObserverControls now,
              where it rightfully belongs — you only hear audio from calls you're
              observing, not calls you're on. */}

          {/* Error display */}
          {error && (
            <div className="flex items-center gap-2 p-2 bg-red-500/20 border border-red-500/50 rounded text-red-400 text-xs">
              <span>{error}</span>
              <button onClick={clearError} className="ml-auto hover:text-white">✕</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
