import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  RefreshCw,
  AlertCircle,
  CheckCircle2,
  XCircle,
  Filter,
  Search,
  ChevronLeft,
  ChevronRight,
  Loader2,
  Phone,
} from 'lucide-react';
import { adminApi } from '../../services/api';
import { logger } from '../../lib/logger';

// =============================================================================
// Webhook Log Tab — admin debugging surface for everything we receive from
// SignalWire (status callbacks, post_prompt, recording-ready, AI events,
// etc.). Use cases:
//   - "Did the post_prompt for call X actually arrive?"
//   - "Why is the conference status missing — is the webhook hitting us?"
//   - "Show me anything that errored in the last hour."
// Backed by the WebhookEvent model + GET /api/admin/webhook-events.
// =============================================================================

interface WebhookEventRow {
  id: number;
  call_id: number | null;
  event_type: string;
  payload: unknown;
  processed: boolean;
  error_message: string | null;
  created_at: string | null;
}

const PER_PAGE = 50;

export function WebhookLogTab() {
  const [events, setEvents] = useState<WebhookEventRow[]>([]);
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  // Filters — applied server-side for correctness across paginated results.
  const [eventTypeFilter, setEventTypeFilter] = useState<string>('');
  const [callIdFilter, setCallIdFilter] = useState<string>('');
  const [processedFilter, setProcessedFilter] = useState<'all' | 'processed' | 'unprocessed'>('all');

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const callIdNum = callIdFilter.trim() ? Number(callIdFilter.trim()) : undefined;
      const params: Parameters<typeof adminApi.listWebhookEvents>[0] = {
        page,
        per_page: PER_PAGE,
      };
      if (eventTypeFilter) params.event_type = eventTypeFilter;
      if (callIdNum && !Number.isNaN(callIdNum)) params.call_id = callIdNum;
      if (processedFilter !== 'all') params.processed = processedFilter === 'processed';

      const res = await adminApi.listWebhookEvents(params);
      setEvents(res.data.events);
      setTotal(res.data.total);
      setHasMore(res.data.has_more);
    } catch (err) {
      logger.error('Failed to load webhook events', err);
    } finally {
      setIsLoading(false);
    }
  }, [page, eventTypeFilter, callIdFilter, processedFilter]);

  // Reset to page 1 whenever a filter changes — otherwise pagination drifts off the result set.
  useEffect(() => {
    setPage(1);
  }, [eventTypeFilter, callIdFilter, processedFilter]);

  useEffect(() => {
    load();
  }, [load]);

  // Lazy-load the dropdown choices once.
  useEffect(() => {
    adminApi
      .listWebhookEventTypes()
      .then((res) => setEventTypes(res.data.event_types))
      .catch((err) => logger.error('Failed to load webhook event types', err));
  }, []);

  const toggleExpanded = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const lastPage = useMemo(() => Math.max(1, Math.ceil(total / PER_PAGE)), [total]);

  return (
    <div className="text-ink">
      {/* Header */}
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h2 className="font-display text-[22px] text-ink leading-none tracking-tightest mb-1">
            Webhook event log
          </h2>
          <p className="text-[13px] text-ink-muted">
            Every callback SignalWire delivers, in arrival order. Filter by call or event type to debug routing, transcription, or post-call signals.
          </p>
        </div>
        <button
          onClick={() => load()}
          disabled={isLoading}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded border border-rule hover:bg-canvas-sunken transition-colors disabled:opacity-50"
          title="Refresh"
        >
          {isLoading ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <RefreshCw className="w-3.5 h-3.5" />
          )}
          Refresh
        </button>
      </div>

      {/* Filters */}
      <div className="mb-4 flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-1.5 text-[12px] text-ink-muted">
          <Filter className="w-3.5 h-3.5" />
          Filters:
        </div>

        <select
          value={eventTypeFilter}
          onChange={(e) => setEventTypeFilter(e.target.value)}
          className="px-2.5 py-1.5 text-xs rounded border border-rule bg-canvas-sunken hover:bg-canvas transition-colors"
        >
          <option value="">All event types</option>
          {eventTypes.map((et) => (
            <option key={et} value={et}>
              {et}
            </option>
          ))}
        </select>

        <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded border border-rule bg-canvas-sunken">
          <Search className="w-3.5 h-3.5 text-ink-muted" />
          <input
            type="number"
            placeholder="Call ID"
            value={callIdFilter}
            onChange={(e) => setCallIdFilter(e.target.value)}
            className="bg-transparent text-xs w-20 focus:outline-none"
          />
        </div>

        <select
          value={processedFilter}
          onChange={(e) => setProcessedFilter(e.target.value as typeof processedFilter)}
          className="px-2.5 py-1.5 text-xs rounded border border-rule bg-canvas-sunken hover:bg-canvas transition-colors"
        >
          <option value="all">All statuses</option>
          <option value="processed">Processed</option>
          <option value="unprocessed">Unprocessed</option>
        </select>

        <div className="ml-auto text-[11px] text-ink-muted font-mono">
          {total.toLocaleString()} events
        </div>
      </div>

      {/* Event list */}
      <div className="rounded-lg border border-rule overflow-hidden">
        {isLoading && events.length === 0 ? (
          <div className="p-12 text-center text-ink-muted">
            <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" />
            <span className="text-xs">Loading events…</span>
          </div>
        ) : events.length === 0 ? (
          <div className="p-12 text-center text-ink-muted">
            <AlertCircle className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No webhook events match the current filters.</p>
          </div>
        ) : (
          <table className="w-full text-[12px]">
            <thead className="bg-canvas-sunken text-ink-muted">
              <tr>
                <th className="text-left px-3 py-2 font-semibold w-32">Received</th>
                <th className="text-left px-3 py-2 font-semibold">Event type</th>
                <th className="text-left px-3 py-2 font-semibold w-28">Call ID</th>
                <th className="text-left px-3 py-2 font-semibold w-28">Status</th>
                <th className="text-right px-3 py-2 font-semibold w-20"></th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => {
                const isOpen = expanded.has(event.id);
                return (
                  <FragmentRow
                    key={event.id}
                    event={event}
                    isOpen={isOpen}
                    onToggle={() => toggleExpanded(event.id)}
                  />
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      {total > PER_PAGE && (
        <div className="mt-4 flex items-center justify-between text-[12px] text-ink-muted">
          <span>
            Page {page} of {lastPage} · showing {events.length}
          </span>
          <div className="flex items-center gap-2">
            <button
              disabled={page === 1 || isLoading}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="flex items-center gap-1 px-2.5 py-1 rounded border border-rule hover:bg-canvas-sunken transition-colors disabled:opacity-40"
            >
              <ChevronLeft className="w-3.5 h-3.5" />
              Prev
            </button>
            <button
              disabled={!hasMore || isLoading}
              onClick={() => setPage((p) => p + 1)}
              className="flex items-center gap-1 px-2.5 py-1 rounded border border-rule hover:bg-canvas-sunken transition-colors disabled:opacity-40"
            >
              Next
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function FragmentRow({
  event,
  isOpen,
  onToggle,
}: {
  event: WebhookEventRow;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const time = event.created_at ? new Date(event.created_at) : null;
  const timeLabel = time
    ? `${time.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${time.toLocaleTimeString()}`
    : '—';

  return (
    <>
      <tr
        className={`border-t border-rule cursor-pointer hover:bg-canvas-sunken/50 transition-colors ${
          isOpen ? 'bg-canvas-sunken/30' : ''
        }`}
        onClick={onToggle}
      >
        <td className="px-3 py-2 font-mono text-[11px] text-ink-muted whitespace-nowrap">
          {timeLabel}
        </td>
        <td className="px-3 py-2 font-mono text-ink">{event.event_type}</td>
        <td className="px-3 py-2 font-mono text-[11px]">
          {event.call_id !== null ? (
            <span className="flex items-center gap-1 text-ink-muted">
              <Phone className="w-3 h-3" />
              {event.call_id}
            </span>
          ) : (
            <span className="text-ink-muted/50">—</span>
          )}
        </td>
        <td className="px-3 py-2">
          {event.error_message ? (
            <span className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-red-900/30 text-red-300">
              <XCircle className="w-3 h-3" />
              error
            </span>
          ) : event.processed ? (
            <span className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-green-900/30 text-green-300">
              <CheckCircle2 className="w-3 h-3" />
              processed
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-yellow-900/30 text-yellow-300">
              pending
            </span>
          )}
        </td>
        <td className="px-3 py-2 text-right text-[11px] text-ink-muted">
          {isOpen ? 'hide' : 'view'}
        </td>
      </tr>
      {isOpen && (
        <tr className="border-t border-rule/50 bg-canvas/40">
          <td colSpan={5} className="px-3 py-3">
            {event.error_message && (
              <div className="mb-2 p-2 rounded bg-red-900/20 border border-red-800/40 text-[11px] text-red-300 font-mono">
                {event.error_message}
              </div>
            )}
            <pre className="text-[11px] text-ink-muted bg-canvas-sunken/60 p-3 rounded border border-rule overflow-x-auto max-h-96">
              {JSON.stringify(event.payload, null, 2)}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}
