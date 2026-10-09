import React, { useState, useEffect } from 'react';
import { Plus, Trash2, RefreshCw } from 'lucide-react';
import { ConfirmationModal } from './ConfirmationModal';
import { ToastNotification } from './ToastNotification';
import { FIELD_LABEL, FORM } from './form';
import { Badge, Button, EmptyState, IconButton, Input, Section, Select, Textarea, type BadgeTone } from '@/components/ui';

/** The refresh glyph while a schema refresh runs — the kit's `IconButton` takes a component, not an element. */
const SpinningRefresh = ({ className }: { className?: string }) => <RefreshCw className={`${className ?? ''} animate-spin text-text`} />;

/** A status word in sentence case ("untested" → "Untested"); the value itself is unchanged. */
const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export interface DBConfig {
    id: string;
    name: string;
    db_type: string;
    connection_string: string;
    description: string;
    schema_info: string;
    last_tested: string | null;
    status: string;
    error_message: string | null;
}

const DB_TYPES = ['postgres', 'mysql', 'sqlite', 'mssql'];

export function DBsTab() {
    const [configs, setConfigs] = useState<DBConfig[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [draftConfig, setDraftConfig] = useState<Partial<DBConfig> | null>(null);
    const [isRefreshing, setIsRefreshing] = useState(false);
    const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
    const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'warning' | 'error' } | null>(null);

    const showToast = (message: string, type: 'success' | 'warning' | 'error' = 'success') => {
        setToast({ show: true, message, type });
        setTimeout(() => setToast(null), 4000);
    };

    useEffect(() => {
        fetchConfigs();
    }, []);

    const fetchConfigs = async () => {
        try {
            const res = await fetch('/api/db-configs');
            if (res.ok) {
                const data = await res.json();
                setConfigs(data);
            }
        } catch (error) {
            console.error('Failed to fetch DB configs', error);
        } finally {
            setIsLoading(false);
        }
    };

    const handleSaveConfig = async () => {
        if (!draftConfig?.name || !draftConfig?.connection_string) {
            showToast('Name and Connection String are required', 'warning');
            return;
        }

        const newConfig = {
            id: draftConfig.id || 'db_' + Date.now(),
            name: draftConfig.name,
            db_type: draftConfig.db_type || 'postgres',
            connection_string: draftConfig.connection_string,
            description: draftConfig.description || '',
            schema_info: draftConfig.schema_info || '',
            last_tested: draftConfig.last_tested || null,
            status: draftConfig.status || 'untested',
            error_message: draftConfig.error_message || null,
        };

        try {
            const res = await fetch('/api/db-configs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newConfig),
            });
            if (res.ok) {
                setDraftConfig(null);
                fetchConfigs();
            } else {
                showToast('Failed to save DB config', 'error');
            }
        } catch (error) {
            console.error('Save error', error);
            showToast('Failed to save DB config', 'error');
        }
    };

    const handleDeleteConfig = async (id: string) => {
        try {
            const res = await fetch(`/api/db-configs/${id}`, { method: 'DELETE' });
            if (res.ok) {
                fetchConfigs();
            }
        } catch (error) {
            showToast('Failed to delete DB config', 'error');
        }
    };

    const handleRefreshSchema = async (id: string, e: React.MouseEvent) => {
        e.stopPropagation();
        setIsRefreshing(true);
        try {
            const res = await fetch(`/api/db-configs/${id}/refresh-schema`, { method: 'POST' });
            if (res.ok) {
                fetchConfigs();
            } else {
                const text = await res.text();
                showToast(`Schema refresh failed: ${text}`, 'error');
            }
        } catch (error) {
            showToast('Schema refresh failed', 'error');
        } finally {
            setIsRefreshing(false);
        }
    };

    // The status tokens, which hold AA in both themes (F239): the kit Badge's
    // success / danger / warning tones, the same tones the chips had.
    const getStatusTone = (status: string): BadgeTone => {
        switch (status) {
            case 'connected': return 'success';
            case 'error': return 'danger';
            default: return 'warning';
        }
    };

    if (isLoading) return <div className="p-8 text-center text-text-faint">Loading...</div>;

    if (draftConfig !== null) {
        return (
            <Section
                variant="plain"
                collapsible={false}
                title={draftConfig.id ? 'Edit database' : 'Add new database'}
                description="Configure a database connection for agent context."
            >
                {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
                <div className={`${FORM} space-y-4`}>
                    <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-1.5">
                            <label htmlFor="db-name" className={FIELD_LABEL}>Name</label>
                            <Input
                                id="db-name"
                                type="text"
                                value={draftConfig.name || ''}
                                onChange={(e) => setDraftConfig({ ...draftConfig, name: e.target.value })}
                                placeholder="e.g. Production DB"
                                autoComplete="off"
                            />
                        </div>
                        <div className="space-y-1.5">
                            <label className={FIELD_LABEL}>Database type</label>
                            <Select
                                value={draftConfig.db_type || 'postgres'}
                                onChange={(v) => setDraftConfig({ ...draftConfig, db_type: v })}
                                aria-label="Database type"
                                options={DB_TYPES.map(t => ({ value: t, label: t }))}
                            />
                        </div>
                    </div>
                    <div className="space-y-1.5">
                        <label htmlFor="db-connection-string" className={FIELD_LABEL}>Connection string</label>
                        <Input
                            id="db-connection-string"
                            type="text"
                            value={draftConfig.connection_string || ''}
                            onChange={(e) => setDraftConfig({ ...draftConfig, connection_string: e.target.value })}
                            className="font-code"
                            placeholder="postgresql://user:password@host:5432/dbname"
                            autoComplete="off"
                        />
                        <p className="text-xs text-text-faint">e.g. postgresql://user:pass@localhost:5432/mydb — stored locally, never sent externally.</p>
                    </div>
                    <div className="space-y-1.5">
                        <label htmlFor="db-description" className={FIELD_LABEL}>Description</label>
                        <Textarea
                            id="db-description"
                            value={draftConfig.description || ''}
                            onChange={(e) => setDraftConfig({ ...draftConfig, description: e.target.value })}
                            className="min-h-[80px]"
                            placeholder="Help the agent understand what this database contains and how it relates to the codebase..."
                        />
                    </div>
                    <div className="flex justify-end gap-2 pt-2">
                        <Button variant="ghost" onClick={() => setDraftConfig(null)}>
                            Cancel
                        </Button>
                        <Button onClick={handleSaveConfig}>
                            Save database
                        </Button>
                    </div>
                </div>
            </Section>
        );
    }

    return (
        <Section
            variant="plain"
            collapsible={false}
            title="Database connections"
            description="Manage database configurations for agent schema context."
            actions={
                <Button size="sm" onClick={() => setDraftConfig({})}>
                    <Plus className="h-4 w-4" /> Add database
                </Button>
            }
        >
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}

            {configs.length === 0 ? (
                <EmptyState
                    className={FORM}
                    title="No databases configured"
                    action={
                        <Button onClick={() => setDraftConfig({})}>
                            <Plus className="h-4 w-4" /> Add database
                        </Button>
                    }
                >
                    Add a database connection to enable schema context for code agents.
                </EmptyState>
            ) : (
                <div className={`${FORM} grid grid-cols-1 gap-3`}>
                    {configs.map((config) => (
                        <div
                            key={config.id}
                            className="group cursor-pointer rounded-lg border border-border bg-surface p-4 transition-colors hover:border-border-strong"
                            onClick={() => setDraftConfig(config)}
                        >
                            <div className="flex items-start justify-between gap-3">
                                <div className="min-w-0">
                                    <h4 className="flex items-center gap-2 text-sm font-semibold text-text">
                                        {/* The card opens on a click; its name does the same for a keyboard (F236). */}
                                        <button
                                            type="button"
                                            onClick={(e) => { e.stopPropagation(); setDraftConfig(config); }}
                                            className="text-left rounded-sm"
                                        >
                                            {config.name}
                                        </button>
                                        <Badge tone={getStatusTone(config.status)} size="sm">
                                            {sentence(config.status)}
                                        </Badge>
                                        <Badge size="sm" className="font-code">
                                            {config.db_type}
                                        </Badge>
                                    </h4>
                                    <p className="mt-1 max-w-lg truncate font-code text-xs text-text-faint">
                                        {config.connection_string.replace(/:\/\/[^@]+@/, '://***@')}
                                    </p>
                                </div>
                                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity">
                                    <IconButton
                                        size="sm"
                                        icon={isRefreshing ? SpinningRefresh : RefreshCw}
                                        onClick={(e) => handleRefreshSchema(config.id, e)}
                                        label={`Refresh the schema of ${config.name}`}
                                        title="Refresh schema"
                                        disabled={isRefreshing}
                                        // Disabled, a click still stops here rather than falling through to open the card.
                                        className="disabled:pointer-events-auto"
                                    />
                                    <IconButton
                                        size="sm"
                                        icon={Trash2}
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            setConfirmDeleteId(config.id);
                                        }}
                                        label={`Delete ${config.name}`}
                                        title="Delete"
                                        className="hover:text-danger"
                                    />
                                </div>
                            </div>
                            {config.description && (
                                <p className="mt-3 line-clamp-2 text-sm text-text-muted">{config.description}</p>
                            )}
                            {config.error_message && (
                                <p className="mt-2 font-code text-xs text-danger">{config.error_message}</p>
                            )}
                            <div className="mt-3 flex gap-1.5 text-xs text-text-faint">
                                {config.schema_info
                                    ? <span>{config.schema_info.split('\n').length - 1} tables cached</span>
                                    : <span>Schema not fetched — hover and click refresh</span>
                                }
                                {config.last_tested && (
                                    <span>· Tested {new Date(config.last_tested).toLocaleString()}</span>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            )}

            <ConfirmationModal
                isOpen={!!confirmDeleteId}
                title="Delete database config"
                message="Are you sure you want to delete this database connection? This action cannot be undone."
                onConfirm={() => {
                    if (confirmDeleteId) handleDeleteConfig(confirmDeleteId);
                    setConfirmDeleteId(null);
                }}
                onClose={() => setConfirmDeleteId(null)}
            />
        </Section>
    );
}
