"""Fast smoke checks for the AI-agent module.

This deliberately avoids instantiating agents: the receptionist loads queue
configuration from the backend during construction. Importing the module and
checking its public agent classes still catches SDK drift and stale imports
without requiring the full Docker stack.
"""

from signalwire import AgentBase

from main_agent import (
    CallCenterTriageAgent,
    OutboundSalesAgent,
    OutboundSupportAgent,
    SalesAISpecialist,
    SupportAISpecialist,
    chunk_text,
)


AGENT_CLASSES = (
    CallCenterTriageAgent,
    SalesAISpecialist,
    SupportAISpecialist,
    OutboundSalesAgent,
    OutboundSupportAgent,
)


def test_agent_classes_use_project_base():
    for agent_class in AGENT_CLASSES:
        assert issubclass(agent_class, AgentBase)


def test_chunk_text_preserves_content_and_limits_chunks():
    text = "One. Two! Three? Four. Five. Six."
    assert chunk_text(text, max_sentences=2) == [
        "One. Two!",
        "Three? Four.",
        "Five. Six.",
    ]


if __name__ == '__main__':
    test_agent_classes_use_project_base()
    test_chunk_text_preserves_content_and_limits_chunks()
    print(f"AI-agent smoke checks passed for {len(AGENT_CLASSES)} agent classes.")
