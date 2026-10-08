/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState } from 'react';
import { cn } from '@/lib/utils';

interface FieldDefinition {
  label: string;
  type: string;
  options?: string[];
  multiple?: boolean;
}

interface CollectDataFormProps {
  data: {
    fields: FieldDefinition[];
  };
  onSubmit: (values: Record<string, any>) => void;
  onCancel?: () => void;
}

export function CollectDataForm({ data, onSubmit, onCancel }: CollectDataFormProps) {
  const [values, setValues] = useState<Record<string, any>>({});
  const [selectedOptions, setSelectedOptions] = useState<Record<string, string[]>>({});
  const [defaultText, setDefaultText] = useState('');

  const hasFields = data.fields.length > 0;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    if (!hasFields) {
      onSubmit({ response: defaultText });
      return;
    }

    // Combine regular values and selected options
    const finalValues: Record<string, any> = {};

    data.fields.forEach((field, idx) => {
      const key = field.label || `field_${idx}`;
      if (field.type === 'options') {
        if (field.multiple) {
          finalValues[key] = selectedOptions[key] || [];
        } else {
          finalValues[key] = values[key] || '';
        }
      } else {
        finalValues[key] = values[key] || '';
      }
    });

    onSubmit(finalValues);
  };

  const setValue = (fieldLabel: string, value: any) => {
    setValues(prev => ({
      ...prev,
      [fieldLabel]: value
    }));
  };

  const toggleOption = (fieldLabel: string, option: string, multiple: boolean) => {
    if (multiple) {
      setSelectedOptions(prev => {
        const current = prev[fieldLabel] || [];
        return {
          ...prev,
          [fieldLabel]: current.includes(option)
            ? current.filter(o => o !== option)
            : [...current, option]
        };
      });
    } else {
      setValue(fieldLabel, option);
    }
  };


  return (
    <div className="rounded-lg border border-border bg-surface-2 p-4">
      <form onSubmit={handleSubmit} className="space-y-4">
        {!hasFields && (
          <textarea
            aria-label="Your response"
            value={defaultText}
            onChange={(e) => setDefaultText(e.target.value)}
            className="w-full rounded-md border border-border-strong bg-surface px-3 py-2 text-sm text-text placeholder:text-text-faint transition-colors resize-none"
            placeholder="Type your response..."
            rows={3}
          />
        )}
        {data.fields.map((field, idx) => {
          const key = field.label || `field_${idx}`;
          
          return (
            <div key={idx}>
              <label htmlFor={`collect-${key}`} className="mb-2 block text-sm font-medium text-text">
                {field.label}
              </label>

              {field.type === 'options' && field.options ? (
                <div className="space-y-2">
                  {field.options.map((option, optIdx) => (
                    <button
                      key={optIdx}
                      type="button"
                      onClick={() => toggleOption(key, option, field.multiple || false)}
                      className={cn(
                        "w-full rounded-md border px-3 py-2 text-left text-sm transition-colors",
                        field.multiple
                          ? (selectedOptions[key] || []).includes(option)
                            ? "border-accent bg-accent-subtle text-accent-ink"
                            : "border-border-strong bg-surface text-text hover:border-text-faint"
                          : values[key] === option
                          ? "border-accent bg-accent-subtle text-accent-ink"
                          : "border-border-strong bg-surface text-text hover:border-text-faint"
                      )}
                    >
                      {option}
                    </button>
                  ))}
                </div>
              ) : (
                <input
                  id={`collect-${key}`}
                  type={
                    field.type === 'text' || field.type === 'email' || field.type === 'phone'
                      ? field.type
                      : field.type === 'number'
                      ? 'number'
                      : field.type === 'date'
                      ? 'date'
                      : 'text'
                  }
                  value={values[key] || ''}
                  onChange={(e) => setValue(key, e.target.value)}
                  className="w-full rounded-md border border-border-strong bg-surface px-3 py-2 text-sm text-text placeholder:text-text-faint transition-colors"
                  placeholder={`Enter ${field.type}...`}
                />
              )}
            </div>
          );
        })}

        <div className="flex gap-2">
          <button
            type="submit"
            className="flex-1 rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-fg transition-colors hover:bg-accent-hover"
            disabled={false}
          >
            Submit
          </button>
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-border-strong px-4 py-2 text-sm text-text-muted transition-colors hover:bg-row-hover hover:text-text"
            >
              Cancel
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
