import { useState } from 'react';
import { X } from 'lucide-react';
import { Contact } from '../../types/callcenter';
import { contactsApi } from '../../services/api';
import { logger } from '../../lib/logger';

interface EditContactModalProps {
  contact: Contact;
  onClose: () => void;
  onSave: (contact: Contact) => void;
}

export function EditContactModal({ contact, onClose, onSave }: EditContactModalProps) {
  const [formData, setFormData] = useState({
    firstName: contact.firstName || '',
    lastName: contact.lastName || '',
    displayName: contact.displayName || '',
    phone: contact.phone || '',
    email: contact.email || '',
    company: contact.company || '',
    jobTitle: contact.jobTitle || '',
    accountTier: contact.accountTier || 'prospect',
    accountStatus: contact.accountStatus || 'active',
    isVip: contact.isVip || false,
    isBlocked: contact.isBlocked || false,
    tags: (contact.tags || []).join(', '),
  });
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);
    setError(null);
    try {
      const response = await contactsApi.update(contact.id, {
        ...formData,
        tags: formData.tags.split(',').map(t => t.trim()).filter(Boolean),
      });
      onSave(response.data);
      onClose();
    } catch (err: any) {
      logger.error('Failed to update contact:', err);
      setError(err.response?.data?.error || 'Failed to update contact');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-canvas-raised rounded-md w-full max-w-lg max-h-[90vh] overflow-y-auto shadow-panel">
        {/* Header */}
        <div className="flex items-center justify-between px-6 pt-5 pb-4 border-b border-rule">
          <div>
            <div className="kicker mb-0.5">Edit</div>
            <h2 className="font-heading text-[20px] font-semibold text-ink leading-none tracking-heading">
              Contact
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="btn-ghost !p-1.5"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-5 space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="First name">
              <input
                type="text"
                value={formData.firstName}
                onChange={(e) => setFormData({ ...formData, firstName: e.target.value })}
                className="input"
              />
            </Field>
            <Field label="Last name">
              <input
                type="text"
                value={formData.lastName}
                onChange={(e) => setFormData({ ...formData, lastName: e.target.value })}
                className="input"
              />
            </Field>
          </div>

          <Field label="Display name" required>
            <input
              type="text"
              value={formData.displayName}
              onChange={(e) => setFormData({ ...formData, displayName: e.target.value })}
              className="input"
              required
            />
          </Field>

          <Field label="Phone" required>
            <input
              type="tel"
              value={formData.phone}
              onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
              className="input mono"
              required
            />
          </Field>

          <Field label="Email">
            <input
              type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              className="input"
            />
          </Field>

          <Field label="Company">
            <input
              type="text"
              value={formData.company}
              onChange={(e) => setFormData({ ...formData, company: e.target.value })}
              className="input"
            />
          </Field>

          <Field label="Job title">
            <input
              type="text"
              value={formData.jobTitle}
              onChange={(e) => setFormData({ ...formData, jobTitle: e.target.value })}
              className="input"
            />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Account tier">
              <select
                value={formData.accountTier}
                onChange={(e) => setFormData({ ...formData, accountTier: e.target.value as any })}
                className="input"
              >
                <option value="prospect">Prospect</option>
                <option value="free">Free</option>
                <option value="pro">Pro</option>
                <option value="enterprise">Enterprise</option>
              </select>
            </Field>
            <Field label="Account status">
              <select
                value={formData.accountStatus}
                onChange={(e) => setFormData({ ...formData, accountStatus: e.target.value as any })}
                className="input"
              >
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
                <option value="suspended">Suspended</option>
              </select>
            </Field>
          </div>

          <Field label="Tags">
            <input
              type="text"
              value={formData.tags}
              onChange={(e) => setFormData({ ...formData, tags: e.target.value })}
              className="input"
              placeholder="vip, priority, enterprise"
            />
          </Field>

          <div className="flex items-center gap-6 pt-1">
            <label className="flex items-center gap-2 text-[13px] text-ink cursor-pointer select-none">
              <input
                type="checkbox"
                checked={formData.isVip}
                onChange={(e) => setFormData({ ...formData, isVip: e.target.checked })}
                className="w-3.5 h-3.5 rounded-sm accent-sw-blue"
              />
              VIP
            </label>
            <label className="flex items-center gap-2 text-[13px] text-ink cursor-pointer select-none">
              <input
                type="checkbox"
                checked={formData.isBlocked}
                onChange={(e) => setFormData({ ...formData, isBlocked: e.target.checked })}
                className="w-3.5 h-3.5 rounded-sm accent-sw-blue"
              />
              Blocked
            </label>
          </div>

          {error && (
            <div className="px-3 py-2 bg-urgent/10 border border-urgent/30 rounded text-urgent-soft text-[12.5px]">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button type="button" onClick={onClose} className="btn-ghost">
              Cancel
            </button>
            <button type="submit" disabled={isSaving} className="btn-primary">
              {isSaving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block kicker mb-1.5">
        {label}{required && <span className="text-sw-fuchsia ml-0.5">*</span>}
      </label>
      {children}
    </div>
  );
}
