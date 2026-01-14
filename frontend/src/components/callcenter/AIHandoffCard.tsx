import React from 'react';
import { motion } from 'framer-motion';
import {
  Brain,
  CheckCircle,
  Target,
  TrendingUp,
  Tag,
  AlertCircle,
  Sparkles
} from 'lucide-react';
import { cn } from '../../lib/utils';
import type { CustomerContext, Sentiment } from '../../types/callcenter';

interface AIHandoffCardProps {
  context: CustomerContext;
}

const SentimentBar: React.FC<{ value: Sentiment; showLabel?: boolean }> = ({
  value,
  showLabel = true
}) => {
  const sentimentColors = {
    very_negative: 'bg-red-500',
    negative: 'bg-orange-500',
    neutral: 'bg-gray-400',
    positive: 'bg-green-500',
    very_positive: 'bg-emerald-500'
  };

  const sentimentLabels = {
    very_negative: 'Very Negative',
    negative: 'Negative',
    neutral: 'Neutral',
    positive: 'Positive',
    very_positive: 'Very Positive'
  };

  const sentiments: Sentiment[] = ['very_negative', 'negative', 'neutral', 'positive', 'very_positive'];

  return (
    <div className="space-y-1">
      {showLabel && (
        <span className="text-xs text-gray-600">{sentimentLabels[value]}</span>
      )}
      <div className="flex space-x-1">
        {sentiments.map((mood) => (
          <div
            key={mood}
            className={cn(
              'h-2 flex-1 rounded',
              mood === value ? sentimentColors[mood] : 'bg-gray-200'
            )}
          />
        ))}
      </div>
    </div>
  );
};

export const AIHandoffCard: React.FC<AIHandoffCardProps> = ({ context }) => {
  if (!context.aiSummary && !context.aiConfidence && !context.extractedInfo?.length) {
    return null;
  }

  const confidenceColor = (context.aiConfidence || 0) >= 80 ? 'text-green-600' :
                          (context.aiConfidence || 0) >= 60 ? 'text-yellow-600' : 'text-red-600';

  const confidenceBg = (context.aiConfidence || 0) >= 80 ? 'bg-green-100' :
                       (context.aiConfidence || 0) >= 60 ? 'bg-yellow-100' : 'bg-red-100';

  return (
    <motion.div
      className="bg-gradient-to-r from-blue-50 to-indigo-50 rounded-lg p-4 border border-indigo-200"
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      {/* Header */}
      <div className="flex items-center mb-3">
        <div className="flex items-center flex-1">
          <div className="p-2 bg-indigo-100 rounded-lg mr-2">
            <Brain className="w-5 h-5 text-indigo-600" />
          </div>
          <span className="font-medium text-gray-900">AI Agent Summary</span>
          <Sparkles className="w-4 h-4 text-indigo-500 ml-1" />
        </div>

        {/* Confidence Badge */}
        {context.aiConfidence !== undefined && (
          <motion.div
            className={cn(
              'px-3 py-1 rounded-full text-sm font-medium',
              confidenceBg, confidenceColor
            )}
            initial={{ scale: 0 }}
            animate={{ scale: 1 }}
            transition={{ delay: 0.2, type: 'spring' }}
          >
            {context.aiConfidence}% Confidence
          </motion.div>
        )}
      </div>

      {/* AI Analysis Section */}
      <div className="space-y-3">
        {/* Intent Detection */}
        {context.aiIntent && (
          <motion.div
            className="flex items-center"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: 0.1 }}
          >
            <Target className="w-4 h-4 text-indigo-600 mr-2" />
            <span className="text-sm text-gray-600 w-20">Intent:</span>
            <span className="px-2 py-1 bg-indigo-100 text-indigo-700 rounded-md text-sm font-medium">
              {context.aiIntent}
            </span>
          </motion.div>
        )}

        {/* Sentiment Analysis */}
        {context.sentiment && (
          <motion.div
            className="flex items-center"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: 0.2 }}
          >
            <TrendingUp className="w-4 h-4 text-indigo-600 mr-2" />
            <span className="text-sm text-gray-600 w-20">Sentiment:</span>
            <div className="flex-1">
              <SentimentBar value={context.sentiment} showLabel />
            </div>
          </motion.div>
        )}

        {/* Extracted Information */}
        {context.extractedInfo && context.extractedInfo.length > 0 && (
          <motion.div
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: 0.3 }}
          >
            <div className="flex items-start">
              <Tag className="w-4 h-4 text-indigo-600 mr-2 mt-1" />
              <div className="flex-1">
                <span className="text-sm text-gray-600 block mb-2">Key Information:</span>
                <div className="flex flex-wrap gap-2">
                  {context.extractedInfo.map((info, index) => (
                    <motion.div
                      key={info.key}
                      className="px-2 py-1 bg-white border border-indigo-200 rounded-md text-xs"
                      initial={{ opacity: 0, scale: 0.8 }}
                      animate={{ opacity: 1, scale: 1 }}
                      transition={{ delay: 0.3 + index * 0.05 }}
                    >
                      <span className="font-medium text-indigo-700">{info.label}:</span>
                      <span className="ml-1 text-gray-700">{info.value}</span>
                      {info.confidence && info.confidence < 80 && (
                        <AlertCircle className="w-3 h-3 text-yellow-500 inline ml-1" />
                      )}
                    </motion.div>
                  ))}
                </div>
              </div>
            </div>
          </motion.div>
        )}

        {/* AI Summary */}
        {context.aiSummary && (
          <motion.div
            className="mt-4 p-3 bg-white rounded border border-indigo-200"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.4 }}
          >
            <p className="text-sm text-gray-700 leading-relaxed">
              {context.aiSummary}
            </p>
          </motion.div>
        )}

        {/* AI Actions Taken */}
        {context.aiActions && context.aiActions.length > 0 && (
          <motion.div
            className="mt-3 pt-3 border-t border-indigo-200"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.5 }}
          >
            <span className="text-xs text-gray-600 font-medium">AI Actions Completed:</span>
            <ul className="mt-2 space-y-1">
              {context.aiActions.map((action, idx) => (
                <motion.li
                  key={idx}
                  className="text-xs text-gray-700 flex items-start"
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: 0.5 + idx * 0.1 }}
                >
                  <CheckCircle className="w-3 h-3 mr-2 text-green-500 mt-0.5" />
                  <span>{action}</span>
                </motion.li>
              ))}
            </ul>
          </motion.div>
        )}
      </div>

      {/* AI Handoff Indicator */}
      <motion.div
        className="mt-4 flex items-center justify-center"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.6 }}
      >
        <div className="flex items-center text-xs text-indigo-600 font-medium">
          <div className="w-2 h-2 bg-indigo-600 rounded-full animate-pulse mr-2" />
          AI to Human Handoff Complete
        </div>
      </motion.div>
    </motion.div>
  );
};