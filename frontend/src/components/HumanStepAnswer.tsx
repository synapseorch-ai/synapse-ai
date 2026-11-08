'use client';

/**
 * Chat's answer to a human step that declared fields.
 *
 * It used `CollectDataForm`, which is the `collect_data` tool's form: it keys
 * each answer by the field's *label* and renders anything it does not know —
 * a yes/no included — as a text box. A human step's answer is keyed by *name*,
 * which is what the engine stores and a later step reads (F75), so the fields
 * come from the one table every form now shares. A step with no fields still
 * gets `CollectDataForm`'s single text box, unchanged.
 */

import { useId, useState } from 'react';
import { Button } from '@/components/ui';
import { HumanFieldsForm } from '@/components/orchestration/HumanFieldsForm';
import { humanAnswer, initialHumanValues, type HumanField } from '@/components/orchestration/humanFields';

export function HumanStepAnswer({
    fields,
    onSubmit,
    disabled,
}: {
    fields: HumanField[];
    onSubmit: (answer: Record<string, unknown>) => void;
    disabled?: boolean;
}) {
    const [values, setValues] = useState(() => initialHumanValues(fields));
    const idPrefix = useId();

    return (
        <form
            className="mt-4 space-y-4 rounded-md border border-border bg-surface p-4"
            onSubmit={(e) => {
                e.preventDefault();
                onSubmit(humanAnswer(fields, values));
            }}
        >
            <HumanFieldsForm fields={fields} values={values} onChange={setValues} idPrefix={idPrefix} />
            <Button type="submit" disabled={disabled}>Submit</Button>
        </form>
    );
}
