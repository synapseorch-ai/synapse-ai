"use client";
/*
 * The destructive-confirm dialog, now a thin adapter over the design system's
 * `ConfirmDialog`.
 *
 * The hand-rolled version handled a backdrop click and nothing else: no focus
 * trap, no scroll lock, no `aria-hidden` on the page behind, no focus returned
 * to whatever opened it, and — because it rendered inline rather than through a
 * portal — any ancestor with `overflow: hidden` could clip it.
 *
 * Kept as a named component with its original props so the six call sites did
 * not have to change; they all inherit the fixed behaviour for free.
 */
import { ConfirmDialog } from '@/components/ui';

interface ConfirmationModalProps {
    isOpen: boolean;
    title: string;
    message: string;
    confirmText?: string;
    cancelText?: string;
    onConfirm: () => void;
    onClose: () => void;
}

export const ConfirmationModal = ({
    isOpen, title, message, confirmText = 'Yes, delete it', onConfirm, onClose,
}: ConfirmationModalProps) => (
    <ConfirmDialog
        open={isOpen}
        onClose={onClose}
        // The original closed itself after confirming, and the call sites rely
        // on that rather than clearing their own state.
        onConfirm={() => { onConfirm(); onClose(); }}
        title={title}
        description={message}
        confirmLabel={confirmText}
    />
);
