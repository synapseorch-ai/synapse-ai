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
    <div className="mt-4 p-4 bg-zinc-950 border border-zinc-800 rounded-md">
      <form onSubmit={handleSubmit} className="space-y-4">
        {!hasFields && (
          <textarea
            aria-label="Your response"
            value={defaultText}
            onChange={(e) => setDefaultText(e.target.value)}
            className="w-full bg-zinc-900 border border-zinc-700 text-white px-4 py-2 text-sm focus:outline-none focus:border-white transition-colors resize-none rounded-md"
            placeholder="Type your response..."
            rows={3}
          />
        )}
        {data.fields.map((field, idx) => {
          const key = field.label || `field_${idx}`;
          
          return (
            <div key={idx}>
              <label htmlFor={`collect-${key}`} className="block text-sm font-medium text-zinc-300 mb-2">
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
                        "w-full text-left px-4 py-2 border text-sm transition-colors",
                        field.multiple
                          ? (selectedOptions[key] || []).includes(option)
                            ? "bg-accent text-accent-fg border-white"
                            : "bg-zinc-900 text-zinc-300 border-zinc-700 hover:border-zinc-500"
                          : values[key] === option
                          ? "bg-accent text-accent-fg border-white"
                          : "bg-zinc-900 text-zinc-300 border-zinc-700 hover:border-zinc-500"
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
                  className="w-full bg-zinc-900 border border-zinc-700 text-white px-4 py-2 text-sm focus:outline-none focus:border-white transition-colors rounded-md"
                  placeholder={`Enter ${field.type}...`}
                />
              )}
            </div>
          );
        })}

        <div className="flex gap-2">
          <button
            type="submit"
            className="flex-1 bg-accent text-accent-fg px-4 py-2 text-sm font-bold uppercase hover:bg-accent-hover transition-colors"
            disabled={false}
          >
            Submit
          </button>
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              className="px-4 py-2 text-sm border border-zinc-700 text-zinc-400 hover:text-white hover:border-white transition-colors"
            >
              Cancel
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
