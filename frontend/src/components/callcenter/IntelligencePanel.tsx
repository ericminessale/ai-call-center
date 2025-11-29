import React from 'react';
import { motion } from 'framer-motion';
import {
  User,
  FileText,
  Clock,
  Tag,
  History,
  AlertCircle,
  Star,
  Phone,
  Mail,
  Calendar,
  Shield
} from 'lucide-react';
import { format } from 'date-fns';
import { cn } from '../../lib/utils';
import { AIHandoffCard } from './AIHandoffCard';
import type { CustomerContext, Call, CallPriority } from '../../types/callcenter';

interface IntelligencePanelProps {
  customerContext: CustomerContext | null;
  activeCall: Call | null;
}

const PriorityBadge: React.FC<{ priority: CallPriority }> = ({ priority }) => {
  const colors = {
    low: 'bg-gray-100 text-gray-700 border-gray-300',
    medium: 'bg-blue-100 text-blue-700 border-blue-300',
    high: 'bg-orange-100 text-orange-700 border-orange-300',
    urgent: 'bg-red-100 text-red-700 border-red-300'
  };

  const icons = {
    low: null,
    medium: null,
    high: <AlertCircle className="w-3 h-3 mr-1" />,
    urgent: <Shield className="w-3 h-3 mr-1" />
  };

  return (
    <span className={cn(
      'inline-flex items-center px-2 py-1 rounded-full text-xs font-medium border',
      colors[priority]
    )}>
      {icons[priority]}
      {priority.toUpperCase()}
    </span>
  );
};

export const IntelligencePanel: React.FC<IntelligencePanelProps> = ({
  customerContext,
  activeCall
}) => {
  if (!customerContext && !activeCall) {
    return (
      <div className="h-full flex items-center justify-center p-6">
        <div className="text-center">
          <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-3">
            <FileText className="w-8 h-8 text-gray-400" />
          </div>
          <p className="text-gray-500">No context available</p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200">
        <h2 className="font-semibold text-gray-900">Intelligence & Context</h2>
      </div>

      {/* Scrollable Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* AI Handoff Card - Top Priority */}
        {customerContext && (
          <AIHandoffCard context={customerContext} />
        )}

        {/* Customer Information */}
        {customerContext && (
          <motion.div
            className="bg-white rounded-lg border border-gray-200 p-4"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
          >
            <div className="flex items-center mb-3">
              <User className="w-4 h-4 text-gray-600 mr-2" />
              <h3 className="font-medium text-gray-900">Customer Information</h3>
              {customerContext.isVip && (
                <span className="ml-2 inline-flex items-center px-2 py-0.5 bg-purple-100 text-purple-700 rounded-full text-xs">
                  <Star className="w-3 h-3 mr-0.5" />
                  VIP
                </span>
              )}
            </div>

            <div className="space-y-2">
              {/* Name */}
              {customerContext.customerName && (
                <div className="flex items-center text-sm">
                  <span className="text-gray-600 w-24">Name:</span>
                  <span className="font-medium text-gray-900">{customerContext.customerName}</span>
                </div>
              )}

              {/* Account Number */}
              {customerContext.accountNumber && (
                <div className="flex items-center text-sm">
                  <span className="text-gray-600 w-24">Account:</span>
                  <span className="font-mono text-gray-900">{customerContext.accountNumber}</span>
                </div>
              )}

              {/* Email */}
              {customerContext.email && (
                <div className="flex items-center text-sm">
                  <Mail className="w-3 h-3 text-gray-500 mr-2" />
                  <span className="text-gray-900">{customerContext.email}</span>
                </div>
              )}

              {/* Phone */}
              {customerContext.phone && (
                <div className="flex items-center text-sm">
                  <Phone className="w-3 h-3 text-gray-500 mr-2" />
                  <span className="text-gray-900">{customerContext.phone}</span>
                </div>
              )}

              {/* Priority */}
              {customerContext.priority && (
                <div className="flex items-center text-sm">
                  <span className="text-gray-600 w-24">Priority:</span>
                  <PriorityBadge priority={customerContext.priority} />
                </div>
              )}
            </div>
          </motion.div>
        )}

        {/* Issue Details */}
        {customerContext?.issueDescription && (
          <motion.div
            className="bg-blue-50 rounded-lg border border-blue-200 p-4"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.2 }}
          >
            <div className="flex items-start">
              <FileText className="w-4 h-4 text-blue-600 mr-2 mt-1" />
              <div>
                <h3 className="font-medium text-blue-900 mb-1">Issue Description</h3>
                <p className="text-sm text-blue-800">{customerContext.issueDescription}</p>
              </div>
            </div>
          </motion.div>
        )}

        {/* Previous Interactions */}
        {customerContext?.previousCalls && customerContext.previousCalls > 0 && (
          <motion.div
            className="bg-white rounded-lg border border-gray-200 p-4"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.3 }}
          >
            <div className="flex items-center mb-3">
              <History className="w-4 h-4 text-gray-600 mr-2" />
              <h3 className="font-medium text-gray-900">Call History</h3>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span className="text-gray-600">Previous Calls:</span>
                <span className="font-medium text-gray-900">{customerContext.previousCalls}</span>
              </div>

              {customerContext.lastCallDate && (
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-600">Last Contact:</span>
                  <span className="font-medium text-gray-900">
                    {format(new Date(customerContext.lastCallDate), 'MMM d, yyyy')}
                  </span>
                </div>
              )}

              <div className="mt-2 p-2 bg-yellow-50 rounded text-xs text-yellow-800">
                <AlertCircle className="w-3 h-3 inline mr-1" />
                Returning customer - review previous interactions
              </div>
            </div>
          </motion.div>
        )}

        {/* Tags */}
        {customerContext?.tags && customerContext.tags.length > 0 && (
          <motion.div
            className="bg-white rounded-lg border border-gray-200 p-4"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.4 }}
          >
            <div className="flex items-center mb-3">
              <Tag className="w-4 h-4 text-gray-600 mr-2" />
              <h3 className="font-medium text-gray-900">Tags</h3>
            </div>

            <div className="flex flex-wrap gap-2">
              {customerContext.tags.map((tag, index) => (
                <span
                  key={index}
                  className="px-2 py-1 bg-gray-100 text-gray-700 rounded-md text-xs"
                >
                  {tag}
                </span>
              ))}
            </div>
          </motion.div>
        )}

        {/* Notes */}
        {customerContext?.notes && customerContext.notes.length > 0 && (
          <motion.div
            className="bg-white rounded-lg border border-gray-200 p-4"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.5 }}
          >
            <div className="flex items-center mb-3">
              <FileText className="w-4 h-4 text-gray-600 mr-2" />
              <h3 className="font-medium text-gray-900">Notes</h3>
            </div>

            <div className="space-y-2">
              {customerContext.notes.map((note, index) => (
                <div key={index} className="text-sm text-gray-700 p-2 bg-gray-50 rounded">
                  {note}
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </div>
    </div>
  );
};