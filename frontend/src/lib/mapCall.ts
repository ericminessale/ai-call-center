import { Call } from '../types/callcenter';

/**
 * Normalizes a call object from the backend (which may send camelCase from REST
 * and snake_case from WebSocket) into the frontend's expected shape.
 *
 * This is the single source of truth for backend→frontend field mapping.
 */
export function mapCall(raw: any): Call {
  return {
    ...raw,
    // Identity
    from_number: raw.from_number || raw.fromNumber,
    phoneNumber: raw.from_number || raw.fromNumber || raw.destination || raw.phoneNumber || 'Unknown',
    signalwire_call_sid: raw.signalwire_call_sid || raw.signalwireCallSid,
    call_sid: raw.signalwire_call_sid || raw.signalwireCallSid || raw.call_sid,

    // Status & type
    status: raw.dashboard_status || raw.status,
    handler_type: raw.handler_type || raw.handlerType || (raw.status === 'ai_active' ? 'ai' : undefined),
    direction: raw.direction,
    destination: raw.destination,

    // Translation state — both casings, since this shim exists precisely
    // because the wire is inconsistent about them.
    caller_language: raw.caller_language || raw.callerLanguage,
    needs_translation: raw.needs_translation ?? raw.needsTranslation ?? false,

    // Contact linkage
    contact_id: raw.contact_id || raw.contactId,

    // AI context
    aiContext: raw.aiContext || raw.ai_context,

    // Queue fields
    queue_id: raw.queue_id || raw.queueId,
    is_urgent: raw.is_urgent || raw.isUrgent,
    queue_status: raw.queue_status || raw.queueStatus,
    assigned_agent_id: raw.assigned_agent_id || raw.assignedAgentId,
    assigned_at: raw.assigned_at || raw.assignedAt,
    conference_name: raw.conference_name || raw.conferenceName,
    wait_time_seconds: raw.wait_time_seconds || raw.waitTimeSeconds,
  };
}

/**
 * Map an array of raw call objects.
 */
export function mapCalls(raw: any[]): Call[] {
  return (raw || []).map(mapCall);
}
