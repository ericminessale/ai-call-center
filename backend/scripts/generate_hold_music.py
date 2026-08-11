#!/usr/bin/env python3
"""Generate the bundled hold-music segment (app/assets/hold_music.wav).

The SWML hold cycle plays this file between queue-position announcements
(see services/queue_dispatch.py). Its LENGTH is load-bearing: one hold
cycle = announcement + this segment, so the file's duration is the cadence
of announcements, the granularity of the hold-timeout check, and the
worst-case lag between an agent being dispatched and the caller joining the
conference. If you regenerate with a different DURATION_SECONDS, update
``queue_dispatch.HOLD_MUSIC_SECONDS`` to match.

Deliberately stdlib-only (wave/math/struct) so it runs anywhere, and
deterministic — no randomness, same bytes every run. 8 kHz mono PCM16
because the PSTN is narrowband anyway; anything above ~3.4 kHz never
reaches the caller.

Usage:  python backend/scripts/generate_hold_music.py
"""

import math
import os
import struct
import wave

SAMPLE_RATE = 8000
DURATION_SECONDS = 20.0
PEAK = 0.55  # of full scale — clearly audible on a call, not blasting

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'app', 'assets', 'hold_music.wav',
)

# Note frequencies (Hz)
A3, B3, C4, D4, E4, F3, G3, G4, B4, A4 = (
    220.00, 246.94, 261.63, 293.66, 329.63, 174.61, 196.00, 392.00, 493.88,
    440.00,
)

# Four gentle chords, 5s each: Cmaj7 → Am7 → Fmaj7 → G6. The last chord
# resolves toward the first so back-to-back segments loop musically.
CHORDS = [
    (C4, E4, G4, B4),
    (A3, C4, E4, G4),
    (F3, A3, C4, E4),
    (G3, B3, D4, E4),
]
CHORD_SECONDS = DURATION_SECONDS / len(CHORDS)
ARP_NOTES_PER_CHORD = 8  # a pluck every 0.625s


def _pad_sample(t_in_chord, chord, t_global):
    """Soft sustained pad: low-level sines with slow attack/release and a
    slight slow tremolo."""
    attack = min(1.0, t_in_chord / 0.8)
    release = min(1.0, (CHORD_SECONDS - t_in_chord) / 0.8)
    env = 0.35 * attack * release
    tremolo = 1.0 + 0.08 * math.sin(2 * math.pi * 0.7 * t_global)
    s = 0.0
    for i, f in enumerate(chord):
        # Slight per-voice detune keeps the pad from sounding like a test tone.
        detune = 1.0 + 0.0007 * (i - 1.5)
        s += math.sin(2 * math.pi * f * detune * t_global)
    return env * tremolo * s / len(chord)


def _arp_sample(t_in_chord, chord, t_global):
    """Plucked arpeggio over the chord: sine + a whisper of 2nd harmonic,
    exponential decay per pluck."""
    step = CHORD_SECONDS / ARP_NOTES_PER_CHORD
    idx = int(t_in_chord / step)
    t_in_note = t_in_chord - idx * step
    # Up-down pattern across the chord tones (0 1 2 3 2 1 0 1)
    pattern = [0, 1, 2, 3, 2, 1, 0, 1]
    f = chord[pattern[idx % len(pattern)]] * 2  # an octave up, above the pad
    decay = math.exp(-4.5 * t_in_note)
    onset = min(1.0, t_in_note / 0.012)  # 12ms attack — no click
    return 0.45 * onset * decay * (
        math.sin(2 * math.pi * f * t_global)
        + 0.15 * math.sin(2 * math.pi * 2 * f * t_global)
    )


def main():
    n = int(SAMPLE_RATE * DURATION_SECONDS)
    frames = bytearray()
    for i in range(n):
        t = i / SAMPLE_RATE
        chord = CHORDS[min(int(t / CHORD_SECONDS), len(CHORDS) - 1)]
        t_in_chord = t - int(t / CHORD_SECONDS) * CHORD_SECONDS
        s = _pad_sample(t_in_chord, chord, t) + _arp_sample(t_in_chord, chord, t)
        # Edge fades so a cycle boundary never clicks.
        if t < 0.3:
            s *= t / 0.3
        if DURATION_SECONDS - t < 0.5:
            s *= (DURATION_SECONDS - t) / 0.5
        val = max(-1.0, min(1.0, s * PEAK))
        frames += struct.pack('<h', int(val * 32767))

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with wave.open(OUT_PATH, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    print(f"wrote {OUT_PATH} ({len(frames)} bytes, {DURATION_SECONDS}s @ {SAMPLE_RATE}Hz)")


if __name__ == '__main__':
    main()
