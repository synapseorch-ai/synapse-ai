import { Button } from '@/components/ui';

/** A settings page's one save, on a rule at the foot of its form (M2g). */
export function SaveBar({ onSave, isSaving, label = 'Save changes', disabled }: {
    onSave: () => void; isSaving?: boolean; label?: string; disabled?: boolean;
}) {
    return (
        <div className="flex justify-end border-t border-border pt-5">
            <Button onClick={onSave} loading={isSaving} disabled={disabled}>
                {isSaving ? 'Saving…' : label}
            </Button>
        </div>
    );
}
