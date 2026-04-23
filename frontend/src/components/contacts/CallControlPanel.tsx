/**
 * CallControlPanel - Expanded real-time call control surface
 *
 * Renders hold, record, TTS, and DTMF controls for active calls.
 * Works for both AI and human agent calls.
 * Demonstrates SignalWire's real-time call control: calls are stateful objects
 * you command by UUID.
 */

import React, { useState, useCallback } from 'react';
import {
  Pause,
  Play,
  Circle,
  Volume2,
  Hash,
  Send,
  Headphones,
  Users,
  ShieldAlert,
  ChevronDown,
  ChevronUp,
  StopCircle,
} from 'lucide-react';
import { callControlApi } from '../../services/api';
import { AudioMonitor } from '../shared/AudioMonitor';

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
  onMonitorStart?: (data: any) => void;
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
  onMonitorStart,
  onBackupRequested,
  onEscalateRequested,
}: CallControlPanelProps) {
  const [showDtmfPad, setShowDtmfPad] = useState(false);
  const [showTtsInput, setShowTtsInput] = useState(false);
  const [ttsText, setTtsText] = useState('');
  const [isLoading, setIsLoading] = useState<string | null>(null); // track which action is loading
  const [expanded, setExpanded] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isMonitoring, setIsMonitoring] = useState(false);
  const [monitorType, setMonitorType] = useState<'tap' | 'conference' | null>(null);

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

  const handleMonitor = useCallback(async () => {
    console.log('[CallControl] handleMonitor called, callId:', callId, 'isMonitoring:', isMonitoring);
    if (isMonitoring) {
      // Stop monitoring
      try {
        await callControlApi.stopMonitor(callId);
      } catch (e: any) {
        // Ignore stop errors
      }
      setIsMonitoring(false);
      setMonitorType(null);
      return;
    }

    setIsLoading('monitor');
    clearError();
    try {
      console.log('[CallControl] Starting monitor for callId:', callId);
      const res = await callControlApi.startMonitor(callId);
      console.log('[CallControl] Monitor response:', res.data);
      setIsMonitoring(true);
      setMonitorType(res.data.monitor_type === 'tap' ? 'tap' : 'conference');
      onMonitorStart?.(res.data);
    } catch (e: any) {
      console.error('[CallControl] Monitor error:', e.response?.status, e.response?.data, e.message);
      setError(e.response?.data?.error || 'Failed to start monitoring');
    } finally {
      setIsLoading(null);
    }
  }, [callId, isMonitoring, onMonitorStart]);

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

            {/* Record - only for human agent calls (AI calls auto-record via SWML) */}
            {isHumanCall && (
              <button
                onClick={handleRecord}
                disabled={isLoading === 'record'}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  isRecording
                    ? 'bg-red-600 hover:bg-red-700 text-white'
                    : 'bg-gray-700 hover:bg-gray-600 text-gray-200'
                } disabled:opacity-50`}
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
              >
                <Hash className="w-3.5 h-3.5" />
                DTMF
              </button>
            )}

            {/* Separator */}
            <div className="w-px h-6 bg-gray-600 mx-1" />

            {/* Monitor button (TODO: re-gate to supervisor/admin in auth overhaul) */}
            <button
              onClick={handleMonitor}
              disabled={isLoading === 'monitor'}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 ${
                isMonitoring
                  ? 'bg-indigo-500 hover:bg-indigo-400 text-white ring-1 ring-indigo-400'
                  : 'bg-indigo-700 hover:bg-indigo-600 text-white'
              }`}
            >
              <Headphones className="w-3.5 h-3.5" />
              {isLoading === 'monitor' ? '...' : isMonitoring ? 'Stop Listen' : 'Listen'}
            </button>

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

          {/* Audio Monitor (shown when tap monitoring is active) */}
          {isMonitoring && monitorType === 'tap' && (
            <AudioMonitor
              callId={String(callId)}
              onClose={() => {
                setIsMonitoring(false);
                setMonitorType(null);
                callControlApi.stopMonitor(callId).catch(() => {});
              }}
            />
          )}

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
