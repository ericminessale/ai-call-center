import { useState, useEffect, useRef, useCallback } from 'react';
import { Headphones, Volume2, VolumeX, X } from 'lucide-react';
import { useSocketContext } from '../../contexts/SocketContext';
import { logger } from '../../lib/logger';

interface AudioMonitorProps {
  callId: string;
  onClose: () => void;
}

/**
 * PCMU (G.711 μ-law) decoder.
 * Converts 8-bit μ-law encoded samples to 16-bit linear PCM.
 */
function decodePCMU(data: Uint8Array): Float32Array {
  const MULAW_BIAS = 33;
  const output = new Float32Array(data.length);

  for (let i = 0; i < data.length; i++) {
    let mulaw = ~data[i] & 0xff;
    const sign = (mulaw & 0x80) ? -1 : 1;
    const exponent = (mulaw >> 4) & 0x07;
    const mantissa = mulaw & 0x0f;

    let sample = ((mantissa << 1) + MULAW_BIAS) << (exponent + 2);
    sample -= MULAW_BIAS << 3;

    // Normalize to -1.0 to 1.0
    output[i] = (sign * sample) / 32768.0;
  }

  return output;
}

/**
 * AudioMonitor component — receives tap audio via Socket.IO and plays it
 * through the Web Audio API. Used for supervisors/admins monitoring calls.
 */
export function AudioMonitor({ callId, onClose }: AudioMonitorProps) {
  const { socket } = useSocketContext();
  const [isPlaying, setIsPlaying] = useState(true);
  const [isMuted, setIsMuted] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [frameCount, setFrameCount] = useState(0);

  const audioContextRef = useRef<AudioContext | null>(null);
  const gainNodeRef = useRef<GainNode | null>(null);
  const nextPlayTimeRef = useRef(0);
  // The tap relay tags every frame with the SignalWire call_id (SID), but this
  // component may be handed either the DB id or the SID as `callId`. Capture the
  // authoritative SID from the tap_joined ack so the frame filter matches —
  // the bug was a DB-id prop compared against SID-tagged frames, so every frame
  // was silently dropped one line into the handler.
  const tapIdRef = useRef<string | null>(callId);

  // Initialize Web Audio API
  const initAudio = useCallback(() => {
    if (audioContextRef.current) return;

    const ctx = new AudioContext({ sampleRate: 8000 });
    const gain = ctx.createGain();
    gain.connect(ctx.destination);
    gain.gain.value = isMuted ? 0 : 1;

    audioContextRef.current = ctx;
    gainNodeRef.current = gain;
    nextPlayTimeRef.current = ctx.currentTime;
  }, [isMuted]);

  // Handle incoming audio frames
  useEffect(() => {
    if (!socket || !isPlaying) return;

    const handleTapAudio = (data: { call_id: string; audio: string; codec: string; sample_rate: number }) => {
      if (data.call_id !== callId && data.call_id !== tapIdRef.current) return;

      // Lazy-init audio context (must be triggered by user interaction, which the "Listen" button provides)
      if (!audioContextRef.current) {
        initAudio();
      }

      const ctx = audioContextRef.current;
      const gain = gainNodeRef.current;
      if (!ctx || !gain) return;

      try {
        // Decode base64 audio
        const binaryString = atob(data.audio);
        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
          bytes[i] = binaryString.charCodeAt(i);
        }

        // Decode PCMU to linear PCM
        const pcmSamples = decodePCMU(bytes);

        // Create audio buffer and schedule playback
        const buffer = ctx.createBuffer(1, pcmSamples.length, data.sample_rate || 8000);
        buffer.getChannelData(0).set(pcmSamples);

        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(gain);

        // Schedule seamless playback
        const now = ctx.currentTime;
        if (nextPlayTimeRef.current < now) {
          nextPlayTimeRef.current = now;
        }
        source.start(nextPlayTimeRef.current);
        nextPlayTimeRef.current += buffer.duration;

        setFrameCount(prev => prev + 1);
      } catch (err) {
        logger.error('[AudioMonitor] Error processing audio frame:', err);
      }
    };

    const handleTapStatus = (data: { call_id: string; status: string }) => {
      if (data.call_id !== callId && data.call_id !== tapIdRef.current) return;
      setIsConnected(data.status === 'connected');
    };

    socket.on('tap_audio', handleTapAudio);
    socket.on('tap_status', handleTapStatus);

    return () => {
      socket.off('tap_audio', handleTapAudio);
      socket.off('tap_status', handleTapStatus);
    };
  }, [socket, callId, isPlaying, initAudio]);

  // RT-01 fix (2026-06-02 audit) + RE-AUDIT-03 stability fix (2026-06-03):
  // tap audio emits are scoped to a server-side ``tap:{signalwire_call_sid}``
  // room. We MUST emit join_tap before audio flows — without it, the
  // server has no way to route tap_audio events to this socket. The
  // server-side handler also validates the user has the right monitor
  // permission (can_listen_ai_calls / can_listen_human_calls) before
  // adding us to the room; failure surfaces as a tap_error event.
  //
  // Kept in its OWN effect with deps [socket, callId] only — the
  // re-audit caught that bundling this into the audio-frame effect
  // (which has initAudio in its deps) caused a join/leave churn on
  // every mute toggle, potentially interrupting the audio stream.
  useEffect(() => {
    if (!socket) return;
    // Reset the authoritative tap id to the prop, then let tap_joined upgrade it
    // to the real SID the relay tags frames with.
    tapIdRef.current = callId;
    const token = localStorage.getItem('access_token');
    socket.emit('join_tap', { token, call_id: callId });

    // Server replies tap_joined { call_id: signalwire_call_sid, db_call_id }.
    // Capture the SID so the frame/status guards accept the relay's frames
    // regardless of whether this component was handed the DB id or the SID.
    //
    // CODE-3 (2026-07-07 pre-deploy): adopt the SID ONLY when the ack is for
    // THIS monitor's call. The ack is broadcast to the joining socket, and
    // with two Listen panels open on one socket both used to adopt whichever
    // ack landed last — collapsing both onto one SID (one call's audio
    // doubled, the other silently dropped). Match on either identifier since
    // `callId` may be the DB id or the SID.
    const handleTapJoined = (d: { call_id?: string; db_call_id?: string | number }) => {
      const isForThisCall =
        d?.call_id === callId || String(d?.db_call_id ?? '') === String(callId);
      if (isForThisCall && d.call_id) tapIdRef.current = d.call_id;
    };
    socket.on('tap_joined', handleTapJoined);

    const handleTapError = (err: { message?: string }) => {
      logger.error('[AudioMonitor] join_tap rejected:', err?.message || err);
    };
    socket.on('tap_error', handleTapError);

    return () => {
      socket.off('tap_joined', handleTapJoined);
      socket.off('tap_error', handleTapError);
      socket.emit('leave_tap', { call_id: callId });
    };
  }, [socket, callId]);

  // Handle mute toggle
  useEffect(() => {
    if (gainNodeRef.current) {
      gainNodeRef.current.gain.value = isMuted ? 0 : 1;
    }
  }, [isMuted]);

  // Cleanup audio context on unmount
  useEffect(() => {
    return () => {
      if (audioContextRef.current) {
        audioContextRef.current.close();
        audioContextRef.current = null;
      }
    };
  }, []);

  const handleClose = () => {
    setIsPlaying(false);
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }
    onClose();
  };

  return (
    <div className="flex items-center gap-2 px-3 py-2 bg-indigo-900/40 border border-indigo-500/30 rounded-lg">
      <Headphones className="w-4 h-4 text-indigo-400" />
      <span className="text-xs text-indigo-300 font-medium">Monitoring</span>

      {/* Connection status indicator */}
      <div className={`w-2 h-2 rounded-full ${isConnected ? 'bg-green-400 animate-pulse' : 'bg-gray-500'}`} />

      {/* Frame counter */}
      <span className="text-[10px] text-indigo-400/70 font-mono">
        {frameCount > 0 ? `${frameCount} frames` : 'waiting...'}
      </span>

      {/* Mute toggle */}
      <button
        onClick={() => setIsMuted(!isMuted)}
        className={`p-1 rounded transition-colors ${isMuted ? 'text-red-400 hover:text-red-300' : 'text-indigo-300 hover:text-indigo-200'}`}
        title={isMuted ? 'Unmute' : 'Mute'}
      >
        {isMuted ? <VolumeX className="w-3.5 h-3.5" /> : <Volume2 className="w-3.5 h-3.5" />}
      </button>

      {/* Close button */}
      <button
        onClick={handleClose}
        className="p-1 text-indigo-400 hover:text-red-400 transition-colors"
        title="Stop monitoring"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}

export default AudioMonitor;
