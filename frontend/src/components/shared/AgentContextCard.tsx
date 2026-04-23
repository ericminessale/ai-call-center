import {
  Bot,
  User,
  Building2,
  AlertTriangle,
  MessageSquare,
  Tag,
  DollarSign,
  Headphones,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { useState } from 'react';

export interface AgentCollectedData {
  customer_name?: string;
  account_number?: string;
  reason?: string;
  issue?: string;
  issue_description?: string;
  urgency?: string;
  priority?: number;
  department?: string;
  interest?: string;
  company?: string;
  budget?: string;
  error_message?: string;
  additional_info?: string;
  ai_summary?: string;
  source_agent?: string;
  preferred_handling?: 'ai' | 'human';
  queue?: string;
  escalated_from?: string;
  escalation_reason?: string;
  global_data?: Record<string, any>;
}

interface AgentContextCardProps {
  context: AgentCollectedData;
  variant?: 'compact' | 'full';
  collapsible?: boolean;
  className?: string;
}

function formatAgentName(raw?: string): string {
  if (!raw) return 'AI Agent';
  return raw.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function urgencyChip(urgency?: string, priority?: number) {
  const isHigh = urgency === 'high' || (priority != null && priority <= 3);
  const isMed = urgency === 'medium' || (priority != null && priority <= 6 && priority > 3);
  if (isHigh) return { chipClass: 'chip-urgent', label: 'High' };
  if (isMed)  return { chipClass: 'chip-wait', label: 'Medium' };
  return { chipClass: 'chip-live', label: 'Low' };
}

function CompactView({ context }: { context: AgentCollectedData }) {
  const issueText = context.issue || context.issue_description || context.reason || context.additional_info;
  const urg = urgencyChip(context.urgency, context.priority);

  return (
    <div className="flex items-start gap-2 text-[12px]">
      <Bot className="w-3.5 h-3.5 text-ai-soft mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          {context.customer_name && (
            <span className="font-medium text-ink">{context.customer_name}</span>
          )}
          {context.company && (
            <span className="text-ink-dim">({context.company})</span>
          )}
          {context.department && (
            <span className="chip chip-info capitalize">{context.department}</span>
          )}
          {context.urgency && (
            <span className={`chip ${urg.chipClass}`}>{urg.label}</span>
          )}
        </div>
        {issueText && (
          <p className="text-ink-dim mt-0.5 line-clamp-1">{issueText}</p>
        )}
      </div>
    </div>
  );
}

function FullView({ context }: { context: AgentCollectedData }) {
  const urg = urgencyChip(context.urgency, context.priority);
  const issueText = context.issue || context.issue_description || context.reason;

  const rows: { icon: React.ReactNode; label: string; value: string; highlight?: boolean }[] = [];

  if (context.customer_name) rows.push({ icon: <User className="w-3.5 h-3.5" />, label: 'Customer', value: context.customer_name });
  if (context.company) rows.push({ icon: <Building2 className="w-3.5 h-3.5" />, label: 'Company', value: context.company });
  if (context.account_number) rows.push({ icon: <Tag className="w-3.5 h-3.5" />, label: 'Account', value: context.account_number });
  if (context.department) rows.push({ icon: <Headphones className="w-3.5 h-3.5" />, label: 'Department', value: context.department });
  if (issueText) rows.push({ icon: <MessageSquare className="w-3.5 h-3.5" />, label: 'Issue', value: issueText, highlight: true });
  if (context.additional_info && context.additional_info !== issueText) rows.push({ icon: <MessageSquare className="w-3.5 h-3.5" />, label: 'Details', value: context.additional_info });
  if (context.interest) rows.push({ icon: <Tag className="w-3.5 h-3.5" />, label: 'Interest', value: context.interest });
  if (context.budget) rows.push({ icon: <DollarSign className="w-3.5 h-3.5" />, label: 'Budget', value: context.budget });
  if (context.error_message) rows.push({ icon: <AlertTriangle className="w-3.5 h-3.5" />, label: 'Error', value: context.error_message });
  if (context.escalation_reason) rows.push({ icon: <AlertTriangle className="w-3.5 h-3.5" />, label: 'Escalation', value: context.escalation_reason });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-[11.5px]">
          <Bot className="w-3.5 h-3.5 text-ai-soft" />
          <span className="kicker" style={{ color: '#B0A4FF' }}>
            {formatAgentName(context.source_agent || context.escalated_from)}
          </span>
          <span className="text-ink-dim">collected</span>
        </div>
        {(context.urgency || context.priority) && (
          <span className={`chip ${urg.chipClass}`}>
            {context.urgency
              ? urg.label
              : context.priority && context.priority <= 3 ? 'High'
              : context.priority && context.priority <= 6 ? 'Medium'
              : 'Low'}
            {context.priority && ` · P${context.priority}`}
          </span>
        )}
      </div>

      <div className="grid gap-1.5">
        {rows.map((row, idx) => (
          <div key={idx} className="flex items-start gap-2.5 text-[13px]">
            <span className="text-ink-dim mt-0.5">{row.icon}</span>
            <span className="text-ink-dim min-w-[88px] flex-shrink-0 text-[11.5px] uppercase tracking-wider">{row.label}</span>
            <span className={`${row.highlight ? 'text-ink font-medium' : 'text-ink-muted'}`}>
              {row.value}
            </span>
          </div>
        ))}
      </div>

      {context.ai_summary && (
        <div className="mt-1 p-2.5 bg-canvas border border-rule rounded text-[12.5px] text-ink-muted italic leading-relaxed">
          &ldquo;{context.ai_summary}&rdquo;
        </div>
      )}
    </div>
  );
}

export function hasContext(context?: AgentCollectedData | null): boolean {
  if (!context) return false;
  return !!(
    context.customer_name ||
    context.reason ||
    context.issue ||
    context.issue_description ||
    context.department ||
    context.urgency ||
    context.interest ||
    context.company ||
    context.budget ||
    context.error_message ||
    context.ai_summary ||
    context.additional_info
  );
}

export function AgentContextCard({
  context,
  variant = 'full',
  collapsible = false,
  className = '',
}: AgentContextCardProps) {
  const [isExpanded, setIsExpanded] = useState(variant === 'full');

  if (!hasContext(context)) return null;

  if (variant === 'compact' && !collapsible) {
    return (
      <div className={`p-2.5 bg-ai/5 border border-ai/25 rounded ${className}`}>
        <CompactView context={context} />
      </div>
    );
  }

  if (collapsible) {
    return (
      <div className={`bg-ai/5 border border-ai/25 rounded overflow-hidden ${className}`}>
        <button
          onClick={() => setIsExpanded(!isExpanded)}
          className="w-full flex items-center justify-between px-3 py-2.5 hover:bg-ai/10 transition-colors text-left"
        >
          <div className="flex items-center gap-2">
            <Bot className="w-3.5 h-3.5 text-ai-soft" />
            <span className="kicker" style={{ color: '#B0A4FF' }}>AI Triage Summary</span>
            {context.department && (
              <span className="chip chip-info capitalize">{context.department}</span>
            )}
            {context.urgency && (
              <span className={`chip ${urgencyChip(context.urgency, context.priority).chipClass}`}>
                {urgencyChip(context.urgency, context.priority).label}
              </span>
            )}
          </div>
          {isExpanded ? <ChevronUp className="w-3.5 h-3.5 text-ink-dim" /> : <ChevronDown className="w-3.5 h-3.5 text-ink-dim" />}
        </button>
        {isExpanded && (
          <div className="px-3 pb-3 border-t border-ai/20">
            <div className="pt-3">
              <FullView context={context} />
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={`p-3 bg-ai/5 border border-ai/25 rounded ${className}`}>
      <FullView context={context} />
    </div>
  );
}
