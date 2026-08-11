"""Caller-language helpers — the single authority for what language a call
is being conducted in, and for keeping live transcription in that language.

Why this exists (2026-08-11, scenario maria_language_memory / Call row 29):
SignalWire ``live_transcribe`` takes exactly one ``lang`` — there is no
auto-detect or multi-language mode — so a Spanish call transcribed as en-US
stores phonetic garbage ("Cuelis Son Los Productos Cafreson"), which poisons
everything downstream: search, summaries, the interaction digest, and the
agent-desktop transcript view. The strategy is therefore:

  1. START transcription in the best language we can KNOW at that moment —
     the per-call fact (``Call.caller_language``) first, else the contact's
     documented language (``Contact.preferred_language``), else en-US.
     Every live_transcribe start site derives through
     :func:`derive_call_language` so they cannot drift.
  2. RESTART the session in the new language when the AI's
     ``set_caller_language`` tool reports a different one mid-call
     (:func:`restart_ai_leg_transcription`, driven by the internal
     ``POST /api/calls/<id>/caller-language`` write-through).

Model-emitted language values (SWAIG tool args, post-prompt summaries) are
unverified input — :func:`normalize_language` is the code gate that turns
them into a real BCP-47 shape or rejects them (PGI: prompts propose, code
decides).
"""

import logging

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = 'en-US'

# Bare primary subtags the model might emit ('es') expanded to the regional
# codes the rest of the system keys on (queue language-matching and the ai
# verb's language list both compare full codes like 'es-ES'). Unknown bare
# primaries are REJECTED rather than guessed — 'en' -> 'en-EN' would be a
# made-up locale, and a wrong region here mislabels transcription for the
# whole call.
_PRIMARY_TO_REGIONAL = {
    'en': 'en-US',
    'es': 'es-ES',
    'fr': 'fr-FR',
    'de': 'de-DE',
    'pt': 'pt-BR',
    'it': 'it-IT',
    'nl': 'nl-NL',
    'ja': 'ja-JP',
    'ko': 'ko-KR',
    'zh': 'zh-CN',
    'pl': 'pl-PL',
    'ru': 'ru-RU',
    'hi': 'hi-IN',
    'ar': 'ar-SA',
}


def normalize_language(value):
    """Coerce a (possibly model-emitted) language value to BCP-47, or None.

    Accepts 'es-ES', 'es-es', 'ES', 'es_ES', 'es'; returns the canonical
    'xx-YY' spelling. Anything that isn't language-code-shaped ('Spanish',
    'null', '') returns None so callers can treat it as absent instead of
    persisting junk.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().replace('_', '-')
    if not raw:
        return None
    parts = raw.split('-')
    primary = parts[0].lower()
    if not (2 <= len(primary) <= 3 and primary.isalpha()):
        return None
    if len(parts) == 1:
        return _PRIMARY_TO_REGIONAL.get(primary)
    region = parts[1].upper()
    if not (len(region) == 2 and region.isalpha()):
        return None
    return f"{primary}-{region}"


def derive_call_language(call, contact=None):
    """Best-known language this call is conducted in (always returns a code).

    Priority: the per-call fact (set by the AI's set_caller_language
    write-through, the queue handoff, or a human starting live-translate),
    then the contact's documented language — which is also the language the
    agent OPENS the call in for known callers (see internal._caller_memory),
    so using it for transcription-from-second-zero is consistent, not a
    guess — then en-US.
    """
    lang = normalize_language(getattr(call, 'caller_language', None))
    if lang:
        return lang

    if contact is None and getattr(call, 'contact_id', None):
        from app import db
        from app.models import Contact
        contact = db.session.get(Contact, call.contact_id)
        # Same cross-workspace refusal as the post-prompt enrichment path
        # (F-02): a mis-bound contact must not leak another tenant's data
        # into this call's behavior.
        if contact is not None and contact.workspace_id != call.workspace_id:
            contact = None

    if contact is not None:
        lang = normalize_language(getattr(contact, 'preferred_language', None))
        if lang:
            return lang

    return DEFAULT_LANGUAGE


def ai_leg_transcribe_start_params(base_url, lang):
    """live_transcribe ``action.start`` params for the caller's A-leg while
    the AI is handling the call. Shared by the initial-call SWML and the
    mid-call language restart so the two can never drift.

    Deliberately NO ai_summary here: the AI post-prompt already summarizes
    the AI phase, and the end-of-call summary belongs to the conference /
    human leg's session (queue_dispatch.py). A second ai_summary on this leg
    produced a duplicate AI-phase fragment that overwrote the whole-call
    note (last-write-wins) — see the 2026-07 CODE-5/CODE-8 notes.
    """
    from app.utils.url_utils import signed_webhook_url
    return {
        "webhook": signed_webhook_url(f"{base_url}/api/webhooks/transcription"),
        "lang": lang,
        "live_events": True,
        "direction": ["remote-caller", "local-caller"],
        # VAD endpointing: ms of silence before an utterance is finalized.
        # SignalWire default is 300ms, which splits on normal mid-sentence
        # pauses (breaths, "um", reading a number). Tune via grep
        # vad_silence_ms (also set in the /start-transcription SWML route +
        # the REST start_transcription); bump higher if still splitting.
        "vad_silence_ms": 800,
    }


def restart_ai_leg_transcription(call, base_url):
    """Stop + start live transcription on the caller leg in the call's
    (newly learned) language. Returns True if a start was issued.

    Restart is the only way to change language — SignalWire has no
    in-session language switch, and a second ``start`` while one is running
    is a no-op (see queues.py /route). Best-effort by design: transcription
    must never take a call down.

    Only runs during the AI phase (``handler_type == 'ai'``). After the
    human handoff the running session carries ``ai_summary: true`` whose
    teardown summary feeds the wrap-up note — killing and replacing that
    session would silently lose the end-of-call summary.

    DEFAULT OFF (2026-08-11, maria_language_memory run 20260811-130839):
    in live verification, every stop+start issued against a leg running
    the ``ai`` verb made transcription events CEASE entirely — no rows in
    any language after the restart — and the affected calls died early
    with their post-prompts never delivered (call rows stuck ai_active,
    no summary, no enrichment). That is the same media-fork seizure
    signature the diagnostics.live_transcribe_enabled kill-switch was
    added to triage. Until SignalWire confirms a safe way to swap
    transcription language mid-``ai``-verb, the restart stays behind
    ``transcription.language_restart_enabled`` (default false): first
    contact with an unknown non-English caller keeps a wrong-language
    transcript for THAT call only — the language gate still captures
    caller_language mid-call, the contact still gets seeded, and every
    subsequent call transcribes correctly from second zero.
    """
    from app.models.system_config import SystemConfig

    if SystemConfig.get(
            'transcription.language_restart_enabled', 'false'
    ).strip().lower() != 'true':
        logger.info(
            "restart_ai_leg_transcription: disabled "
            "(transcription.language_restart_enabled != true) — call %s "
            "keeps its current transcription language", call.id,
        )
        return False

    # Same diagnostic kill-switch as the initial-call start (2026-06-11
    # media-fork triage): when live_transcribe is disabled for A/B calls,
    # don't sneak one in through the language path.
    if SystemConfig.get(
            'diagnostics.live_transcribe_enabled', 'true'
    ).strip().lower() != 'true':
        logger.info(
            "restart_ai_leg_transcription: live_transcribe disabled by "
            "diagnostics kill-switch — skipping (call %s)", call.id,
        )
        return False

    if call.handler_type != 'ai':
        logger.info(
            "restart_ai_leg_transcription: call %s is in the human phase — "
            "leaving the ai_summary-bearing session alone", call.id,
        )
        return False

    sid = call.signalwire_call_sid
    if not sid:
        return False

    from app.services.signalwire_api import get_signalwire_api
    api = get_signalwire_api()
    lang = derive_call_language(call)
    params = ai_leg_transcribe_start_params(base_url, lang)

    try:
        # Stop failure is expected when nothing is running (coach.py has the
        # same tolerance) — the start below then simply begins a session.
        api.stop_transcription(sid)
    except Exception as e:
        logger.info(
            "restart_ai_leg_transcription: stop on %s failed (probably not "
            "running): %s", sid, e,
        )

    try:
        api.live_transcribe_start(sid, params)
        logger.info(
            "restart_ai_leg_transcription: call %s now transcribing as %s",
            call.id, lang,
        )
        return True
    except Exception as e:
        logger.warning(
            "restart_ai_leg_transcription: start on %s failed (non-fatal — "
            "transcript stays in the previous language): %s", sid, e,
        )
        return False
