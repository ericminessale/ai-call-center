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
  /** compact = inline summary, full = expanded detail view */
  variant?: 'compact' | 'full';
  /** Allow toggling between compact and full */
  collapsible?: boolean;
  className?: string;
}

/** Formats a source_agent string like "call_center_triage" to "Call Center Triage" */
function formatAgentName(raw?: string): string {
  if (!raw) return 'AI Agent';
  return raw
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

/** Returns urgency color classes */
function urgencyStyle(urgency?: string, priority?: number) {
  const isHigh = urgency === 'high' || (priority != null && priority <= 3);
  const isMed = urgency === 'medium' || (priority != null && priority <= 6 && priority > 3);
  if (isHigh) return { bg: 'bg-red-500/20', text: 'text-red-400', border: 'border-red-500/30', label: 'High' };
  if (isMed) return { bg: 'bg-yellow-500/20', text: 'text-yellow-400', border: 'border-yellow-500/30', label: 'Medium' };
  return { bg: 'bg-green-500/20', text: 'text-green-400', border: 'border-green-500/30', label: 'Low' };
}

/** Compact inline version for queue cards and banners */
function CompactView({ context }: { context: AgentCollectedData }) {
  const issueText = context.issue || context.issue_description || context.reason || context.additional_info;
  const urg = urgencyStyle(context.urgency, context.priority);

  return (
    <div className="flex items-start gap-2 text-xs">
      <Bot className="w-3.5 h-3.5 text-purple-400 mt-0.5 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          {context.customer_name && (
            <span className="font-medium text-white">{context.customer_name}</span>
          )}
          {context.company && (
            <span className="text-gray-400">({context.company})</span>
          )}
          {context.department && (
            <span className="px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-300 capitalize">
              {context.department}
            </span>
          )}
          {context.urgency && (
            <span className={`px-1.5 py-0.5 rounded ${urg.bg} ${urg.text}`}>
              {urg.label}
            </span>
          )}
        </div>
        {issueText && (
          <p className="text-gray-400 mt-0.5 line-clamp-1">{issueText}</p>
        )}
      </div>
    </div>
  );
}

/** Full detail view for the live call panel */
function FullView({ context }: { context: AgentCollectedData }) {
  const urg = urgencyStyle(context.urgency, context.priority);
  const issueText = context.issue || context.issue_description || context.reason;

  // Collect all non-null fields for display
  const rows: { icon: React.ReactNode; label: string; value: string; highlight?: boolean }[] = [];

  if (context.customer_name) {
    rows.push({ icon: <User className="w-3.5 h-3.5" />, label: 'Customer', value: context.customer_name });
  }
  if (context.company) {
    rows.push({ icon: <Building2 className="w-3.5 h-3.5" />, label: 'Company', value: context.company });
  }
  if (context.account_number) {
    rows.push({ icon: <Tag className="w-3.5 h-3.5" />, label: 'Account', value: context.account_number });
  }
  if (context.department) {
    rows.push({ icon: <Headphones className="w-3.5 h-3.5" />, label: 'Department', value: context.department });
  }
  if (issueText) {
    rows.push({ icon: <MessageSquare className="w-3.5 h-3.5" />, label: 'Issue', value: issueText, highlight: true });
  }
  if (context.additional_info && context.additional_info !== issueText) {
    rows.push({ icon: <MessageSquare className="w-3.5 h-3.5" />, label: 'Details', value: context.additional_info });
  }
  if (context.interest) {
    rows.push({ icon: <Tag className="w-3.5 h-3.5" />, label: 'Interest', value: context.interest });
  }
  if (context.budget) {
    rows.push({ icon: <DollarSign className="w-3.5 h-3.5" />, label: 'Budget', value: context.budget });
  }
  if (context.error_message) {
    rows.push({ icon: <AlertTriangle className="w-3.5 h-3.5" />, label: 'Error', value: context.error_message });
  }
  if (context.escalation_reason) {
    rows.push({ icon: <AlertTriangle className="w-3.5 h-3.5" />, label: 'Escalation', value: context.escalation_reason });
  }

  return (
    <div className="space-y-2">
      {/* Header row: agent attribution + urgency */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-gray-400">
          <Bot className="w-4 h-4 text-purple-400" />
          <span className="font-medium text-purple-300">
            {formatAgentName(context.source_agent || context.escalated_from)}
          </span>
          <span>collected</span>
        </div>
        {(context.urgency || context.priority) && (
          <span className={`px-2 py-0.5 rounded text-xs font-semibold ${urg.bg} ${urg.text} border ${urg.border}`}>
            {context.urgency
              ? urg.label
              : context.priority && context.priority <= 3 ? 'High'
              : context.priority && context.priority <= 6 ? 'Medium'
              : 'Low'}
            {context.priority && ` (${context.priority})`}
          </span>
        )}
      </div>

      {/* Data rows */}
      <div className="grid gap-1.5">
        {rows.map((row, idx) => (
          <div key={idx} className="flex items-start gap-2 text-sm">
            <span className="text-gray-500 mt-0.5">{row.icon}</span>
            <span className="text-gray-400 min-w-[80px] flex-shrink-0">{row.label}</span>
            <span className={`text-white ${row.highlight ? 'font-medium' : ''}`}>
              {row.value}
            </span>
          </div>
        ))}
      </div>

      {/* AI Summary if available */}
      {context.ai_summary && (
        <div className="mt-2 p-2 bg-gray-900/50 rounded-md text-sm text-gray-300 italic">
          &ldquo;{context.ai_summary}&rdquo;
        </div>
      )}
    </div>
  );
}

/** Check if context has any meaningful data worth showing */
export function hasContext(context?: AgentCollectedData | null): boolean {
  if (!context) return false;
  // Check for any non-empty meaningful field
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
      <div className={`p-2 bg-purple-900/20 border border-purple-700/40 rounded-lg ${className}`}>
        <CompactView context={context} />
      </div>
    );
  }

  if (collapsible) {
    return (
      <div className={`bg-purple-900/20 border border-purple-700/40 rounded-lg overflow-hidden ${className}`}>
        <button
          onClick={() => setIsExpanded(!isExpanded)}
          className="w-full flex items-center justify-between p-3 hover:bg-purple-900/10 transition-colors text-left"
        >
          <div className="flex items-center gap-2 text-sm">
            <Bot className="w-4 h-4 text-purple-400" />
            <span className="font-medium text-purple-300">AI Triage Summary</span>
            {context.department && (
              <span className="px-1.5 py-0.5 text-[10px] rounded bg-blue-500/20 text-blue-300 capitalize">
                {context.department}
              </span>
            )}
            {context.urgency && (
              <span className={`px-1.5 py-0.5 text-[10px] rounded ${urgencyStyle(context.urgency, context.priority).bg} ${urgencyStyle(context.urgency, context.priority).text}`}>
                {urgencyStyle(context.urgency, context.priority).label}
              </span>
            )}
          </div>
          {isExpanded ? (
            <ChevronUp className="w-4 h-4 text-gray-400" />
          ) : (
            <ChevronDown className="w-4 h-4 text-gray-400" />
          )}
        </button>
        {isExpanded && (
          <div className="px-3 pb-3">
            <FullView context={context} />
          </div>
        )}
      </div>
    );
  }

  // Default: full, non-collapsible
  return (
    <div className={`p-3 bg-purple-900/20 border border-purple-700/40 rounded-lg ${className}`}>
      <FullView context={context} />
    </div>
  );
}
