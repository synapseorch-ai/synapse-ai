import { CheckCircle, AlertCircle, XCircle } from 'lucide-react';

type ToastType = 'success' | 'warning' | 'error';

interface ToastProps {
    show: boolean;
    message: string;
    type?: ToastType;
}

/** The toast is one surface in the text colour; the meaning is its icon's (UX1: quiet success). */
const iconStyles: Record<ToastType, string> = {
    success: 'text-success',
    warning: 'text-warning',
    error:   'text-danger',
};

const ToastIcon: Record<ToastType, React.ElementType> = {
    success: CheckCircle,
    warning: AlertCircle,
    error:   XCircle,
};

export const ToastNotification = ({ show, message, type = 'success' }: ToastProps) => {
    if (!show) return null;
    const Icon = ToastIcon[type];
    return (
        <div role="status" className="fixed left-1/2 top-6 z-[100] flex -translate-x-1/2 items-center gap-2.5 rounded-md border border-border bg-surface px-4 py-2.5 text-xs font-medium text-text shadow-[var(--shadow)] animate-in fade-in slide-in-from-top-4 duration-300">
            <Icon className={`size-4 shrink-0 ${iconStyles[type]}`} aria-hidden />
            <span>{message}</span>
        </div>
    );
};
