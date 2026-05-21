"""AI Coach (sidecar) — attach/detach a SignalWire ai_sidecar to a live call.

Companion to the Knowledge Factbook (`kb_factbook.py`). Both are Agent Assist
surfaces; this one runs an LLM and bills per-minute via ``ai_sidecar_per_minute``,
the Factbook is pure pgvector retrieval and free.

Capability gate
---------------
Admin policy: ``can_use_coach`` permission flag (in PERMISSION_FLAGS). Sets
the ceiling — if the agent doesn't have this, the panel never renders and
the attach endpoint refuses.

Per-call mode (agent decision)
------------------------------
The agent picks a mode per-call via the LiveCallTab Coach panel header:
  - ``off``         → no sidecar attached at all (no billing, panel collapsed)
  - ``on_request``  → sidecar attached, prompt biases toward staying silent;
                      agent uses "Ask coach" to fire ``ai-sidecar … ask``
  - ``auto``        → sidecar suggests on every customer turn-end

Mode changes mid-call detach + re-attach with the new prompt. Detach when
agent picks ``off`` or hangs up.

Style (user preference)
-----------------------
``user.coach_intensity`` is the agent's prompt-tone preset:
  - ``terse``    → one short tip per suggestion
  - ``standard`` → balanced suggestion with a brief why
  - ``verbose`` → full reasoning + suggested phrasing script

Architecture
------------
Sidecar attaches to the caller's leg via the ``calling.ai_sidecar`` REST
command (mirrors how ``live_transcribe`` is started). It coexists with the
already-running ``live_transcribe`` — per dev Q1, the Factbook transcription
pipeline keeps working alongside the sidecar.

Sidecar events POST to ``/api/webhooks/sidecar/events``. ``global_data``
carries our agent_user_id / queue_slug / mode / intensity through to every
event so the webhook can route the resulting Socket.IO emit to the right
agent room without a lookup.

See AGENT_ASSIST.md "Feature 2 — AI Coach (sidecar)" for the full spec.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt presets
# ---------------------------------------------------------------------------
# Each intensity preset is a STYLE block appended to a shared base prompt and
# a mode-specific tail. The base describes the sidecar's role and the ground
# rules; the tail switches between auto and on_request defaults. Kept here
# (not in a separate file) so the whole decision/prompt pipeline reads top-
# to-bottom in one place.

_INTENSITY_PRESETS = {
    'terse': (
        "STYLE: One short tip per suggestion, under 12 words. No preamble, "
        "no reasoning, no signoff. Plain imperative phrasing. "
        "Examples:\n"
        "  - \"Offer the loyalty discount.\"\n"
        "  - \"Confirm the account number before continuing.\"\n"
        "  - \"Acknowledge their frustration before answering.\""
    ),
    'standard': (
        "STYLE: A balanced suggestion in 1–2 sentences. Lead with the "
        "actionable move; include a brief why if it isn't obvious from the "
        "suggestion itself. No filler, no apologies.\n"
        "Example: \"Offer the 20% loyalty discount — they mentioned "
        "comparing competitor pricing, so price sensitivity is on the table.\""
    ),
    'verbose': (
        "STYLE: Full reasoning, then a suggested phrasing the agent can "
        "read verbatim. Structure your output as:\n"
        "  Why: <one sentence explaining the situation read>\n"
        "  Try: \"<verbatim phrasing for the agent to speak>\"\n"
        "Use this format consistently so the agent can scan it at a glance."
    ),
}

_BASE_PROMPT = (
    "You are a silent AI coach attached as a sidecar to a live call between "
    "a human call-center agent and a customer. You hear both sides of the "
    "conversation in real time.\n\n"
    "ROLE: surface concise, actionable suggestions to the agent only. The "
    "customer NEVER hears you. The agent reads your output as text in their "
    "console — speak to them, not to the customer.\n\n"
    "GROUND RULES:\n"
    "1. Never write what the customer should hear directly unless asked for "
    "a verbatim phrasing — and even then, address the agent.\n"
    "2. Stay specific. Reference the customer's actual words when possible.\n"
    "3. Do not editorialize about the agent's performance. Suggest, don't grade.\n"
    "4. If you don't have enough signal to add value, call sidecar_skip "
    "rather than emitting a low-quality suggestion.\n"
    "5. Use the lookup_kb tool when the customer asks about a product, "
    "policy, or fact that isn't obvious from the conversation. Don't guess."
)

_ON_REQUEST_TAIL = (
    "DEFAULT BEHAVIOR: STAY SILENT. Call sidecar_skip on every customer "
    "turn-end. ONLY emit a suggestion when you receive an explicit 'ask' "
    "event from the agent. Treat the ask text as a direct question and "
    "respond in your configured STYLE."
)

_AUTO_TAIL = (
    "DEFAULT BEHAVIOR: After each customer turn-end, decide whether a "
    "suggestion would help the agent right now. Bias toward fewer, higher-"
    "quality suggestions — if the agent is handling the call well, call "
    "sidecar_skip. Aim for at most one suggestion per ~30 seconds of "
    "conversation unless something material changes (objection, escalation "
    "trigger, factual question)."
)


def _build_prompt(coach_mode: str, coach_intensity: str) -> str:
    """Compose the sidecar system prompt from base + intensity + mode tail.

    Returns a single string; the sidecar verb takes a flat prompt body.
    """
    intensity_block = _INTENSITY_PRESETS.get(
        coach_intensity, _INTENSITY_PRESETS['standard']
    )
    tail = _ON_REQUEST_TAIL if coach_mode == 'on_request' else _AUTO_TAIL
    return f"{_BASE_PROMPT}\n\n{intensity_block}\n\n{tail}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

VALID_MODES = ('off', 'on_request', 'auto')


def is_active_mode(mode: str) -> bool:
    """Return True if this mode requires the sidecar to be attached.

    ``off`` is the only non-active mode. Unknown values are treated as
    non-active so a typo can't accidentally start billing.
    """
    return mode in ('on_request', 'auto')


def build_sidecar_start_params(
    agent,
    *,
    call,
    mode: str,
    queue_slug: str = '',
    base_url: str,
) -> Dict[str, Any]:
    """Build the ``params`` body for a ``calling.ai_sidecar`` start command.

    Shape mirrors ``calling.live_transcribe`` start — ``action.start`` wraps
    the verb body. The webhook URL is what the sidecar webhook consumes;
    ``global_data`` rides on every emitted event so the webhook can route
    by agent_user_id without a DB lookup.

    Args:
        agent: User row triggering the attach. Source of `coach_intensity`
            (style preset) and identity for global_data routing.
        call: Call ORM row — used for ``call_id`` correlation in global_data.
        mode: per-call mode picked by the agent — ``'on_request'`` or
            ``'auto'``. ``'off'`` shouldn't reach here; callers gate with
            :func:`is_active_mode` first.
        queue_slug: optional queue slug for event payload context.
        base_url: public origin for webhook URL construction.
    """
    from app.utils.url_utils import signed_webhook_url

    coach_intensity = getattr(agent, 'coach_intensity', None) or 'standard'
    prompt = _build_prompt(mode, coach_intensity)

    # SWAIG tools the sidecar can invoke during the call. Currently just
    # lookup_kb (M11) — wraps the same pgvector retrieval pipeline the
    # Factbook uses, but driven by the sidecar's own judgement rather than
    # the agent's button-press. Backed by /api/webhooks/coach/lookup_kb.
    swaig_functions = [
        {
            "function": "lookup_kb",
            "purpose": (
                "Look up facts from the agent's knowledge base when the "
                "customer asks a product/policy/factual question whose "
                "answer isn't in the running conversation. Do NOT use for "
                "small talk or open-ended objections."
            ),
            "argument": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Short topic or question to search for. Use the "
                            "customer's own keywords when possible."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "How many facts to return (1–10, default 3). "
                            "Prefer 3 unless the topic is broad."
                        ),
                    },
                },
                "required": ["query"],
            },
            "web_hook_url": signed_webhook_url(
                f"{base_url}/api/webhooks/coach/lookup_kb"
            ),
        },
    ]

    return {
        "action": {
            "start": {
                "prompt": prompt,
                "webhook": signed_webhook_url(
                    f"{base_url}/api/webhooks/sidecar/events"
                ),
                # global_data rides on every sidecar event back to our webhook
                # (per dev Q4). Keys here drive M8's routing without a DB hit.
                "global_data": {
                    "agent_user_id": agent.id,
                    "agent_name": agent.name or agent.email,
                    "agent_email": agent.email,
                    "queue_slug": queue_slug,
                    "coach_mode": mode,
                    "coach_intensity": coach_intensity,
                    "call_sid": call.signalwire_call_sid,
                    "call_db_id": call.id,
                },
                "swaig_functions": swaig_functions,
            }
        }
    }


def attach_sidecar_to_call(
    *,
    call,
    agent,
    mode: str,
    queue_slug: str = '',
    base_url: str,
) -> None:
    """Attach a sidecar with the given mode to the caller's leg.

    Raises on failure — the agent-facing attach endpoint reports the error
    back to the agent so they can retry. (This differs from the prior
    auto-attach-on-dispatch behavior, where failures had to be swallowed
    silently to avoid breaking call routing.)

    Args:
        call: Call ORM row with a live ``signalwire_call_sid``.
        agent: User row whose intensity preset feeds the prompt.
        mode: ``'on_request'`` or ``'auto'``. Callers must gate ``'off'``
            with :func:`is_active_mode` and call :func:`detach_sidecar_from_call`
            instead.
        queue_slug: optional queue context, surfaces in event payloads.
        base_url: public origin for webhook URLs.
    """
    if not is_active_mode(mode):
        raise ValueError(f"attach_sidecar_to_call called with non-active mode '{mode}'")

    from app.services.signalwire_api import SignalWireAPI

    params = build_sidecar_start_params(
        agent, call=call, mode=mode, queue_slug=queue_slug, base_url=base_url,
    )

    api = SignalWireAPI()
    api.start_ai_sidecar(call.signalwire_call_sid, params)
    logger.info(
        f"AI Coach attached: call={call.signalwire_call_sid} "
        f"agent={agent.id} ({agent.email}) mode={mode} "
        f"intensity={getattr(agent, 'coach_intensity', 'standard')}"
    )


def detach_sidecar_from_call(call) -> None:
    """Detach any attached sidecar from this call.

    Idempotent — SignalWire returns a benign error if no sidecar is
    attached; ``SignalWireAPI.stop_ai_sidecar`` logs and swallows it.
    """
    from app.services.signalwire_api import SignalWireAPI

    api = SignalWireAPI()
    api.stop_ai_sidecar(call.signalwire_call_sid)
    logger.info(f"AI Coach detached: call={call.signalwire_call_sid}")
