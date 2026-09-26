import React, { useState, useEffect } from 'react';
import { Plus, Trash2, Database, RefreshCw } from 'lucide-react';
import { ConfirmationModal } from './ConfirmationModal';
import { ToastNotification } from './ToastNotification';
import { Select } from '@/components/ui';

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

    // The status tokens, which hold AA in both themes on their -subtle grounds.
    // Raw 500s on a tint of themselves read 4.16:1 in light ("untested"), and
    // emerald and red 500 had no light inversion at all (F239).
    const getStatusColor = (status: string) => {
        switch (status) {
            case 'connected': return 'bg-success-subtle text-success border-success/30';
            case 'error': return 'bg-danger-subtle text-danger border-danger/30';
            default: return 'bg-warning-subtle text-warning border-warning/30';
        }
    };

    if (isLoading) return <div className="p-8 text-center text-text-faint">Loading...</div>;

    if (draftConfig !== null) {
        return (
            <div className="space-y-8">
                {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
                <div className="mb-4">
                    <h3 className="text-lg font-bold text-white flex items-center gap-2">
                        <Database className="h-5 w-5" />
                        {draftConfig.id ? 'Edit Database' : 'Add New Database'}
                    </h3>
                    <p className="text-text-faint text-sm mt-1">Configure a database connection for agent context.</p>
                </div>

                <div className="space-y-4">
                    <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                            <label htmlFor="db-name" className="text-xs uppercase font-bold text-text-faint tracking-wider">Name</label>
                            <input
                                id="db-name"
                                type="text"
                                value={draftConfig.name || ''}
                                onChange={(e) => setDraftConfig({ ...draftConfig, name: e.target.value })}
                                className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono rounded-md"
                                placeholder="e.g. Production DB"
                                autoComplete="off"
                            />
                        </div>
                        <div className="space-y-2">
                            <label className="text-xs uppercase font-bold text-text-faint tracking-wider">Database Type</label>
                            <Select
                                value={draftConfig.db_type || 'postgres'}
                                onChange={(v) => setDraftConfig({ ...draftConfig, db_type: v })}
                                aria-label="Database type"
                                options={DB_TYPES.map(t => ({ value: t, label: t }))}
                            />
                        </div>
                    </div>
                    <div className="space-y-2">
                        <label htmlFor="db-connection-string" className="text-xs uppercase font-bold text-text-faint tracking-wider">Connection String</label>
                        <input
                            id="db-connection-string"
                            type="text"
                            value={draftConfig.connection_string || ''}
                            onChange={(e) => setDraftConfig({ ...draftConfig, connection_string: e.target.value })}
                            className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono rounded-md"
                            placeholder="postgresql://user:password@host:5432/dbname"
                            autoComplete="off"
                        />
                        <p className="text-[10px] text-text-faint">e.g. postgresql://user:pass@localhost:5432/mydb — stored locally, never sent externally.</p>
                    </div>
                    <div className="space-y-2">
                        <label htmlFor="db-description" className="text-xs uppercase font-bold text-text-faint tracking-wider">Description</label>
                        <textarea
                            id="db-description"
                            value={draftConfig.description || ''}
                            onChange={(e) => setDraftConfig({ ...draftConfig, description: e.target.value })}
                            className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono min-h-[80px] rounded-md"
                            placeholder="Help the agent understand what this database contains and how it relates to the codebase..."
                        />
                    </div>
                    <div className="flex gap-4 justify-end pt-4">
                        <button
                            onClick={() => setDraftConfig(null)}
                            className="px-6 py-2.5 text-sm font-bold text-zinc-400 hover:text-white transition-all"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={handleSaveConfig}
                            className="px-6 py-2.5 text-sm font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all shadow-lg"
                        >
                            Save Database
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="space-y-8">
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
            <div className="mb-4 flex items-center justify-between">
                <div>
                    <h3 className="text-lg font-bold text-white flex items-center gap-2">
                        <Database className="h-5 w-5" />
                        Database Connections
                    </h3>
                    <p className="text-text-faint text-sm mt-1">Manage database configurations for agent schema context.</p>
                </div>
                <button
                    onClick={() => setDraftConfig({})}
                    className="flex items-center gap-2 px-4 py-2 text-sm font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all shadow-lg"
                >
                    <Plus className="w-4 h-4" /> Add Database
                </button>
            </div>

            {configs.length === 0 ? (
                <div className="text-center py-12 border border-dashed border-zinc-800 bg-zinc-900/50 rounded-md">
                    <Database className="w-8 h-8 mx-auto text-text-faint mb-3" />
                    <h3 className="text-sm font-bold text-zinc-100">No databases configured</h3>
                    <p className="text-sm text-text-faint mt-1 mb-6">Add a database connection to enable schema context for code agents.</p>
                    <button
                        onClick={() => setDraftConfig({})}
                        className="inline-flex items-center gap-2 px-6 py-2.5 text-sm font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all shadow-lg"
                    >
                        <Plus className="w-4 h-4" /> Add Database
                    </button>
                </div>
            ) : (
                <div className="grid grid-cols-1 gap-4">
                    {configs.map((config) => (
                        <div
                            key={config.id}
                            className="p-4 border border-zinc-800 bg-zinc-900/50 hover:border-zinc-600 transition-colors cursor-pointer group rounded-md"
                            onClick={() => setDraftConfig(config)}
                        >
                            <div className="flex justify-between items-start mb-2">
                                <div>
                                    <h4 className="font-bold text-white text-lg flex items-center gap-2">
                                        {/* The card opens on a click; its name does the same for a keyboard (F236). */}
                                        <button
                                            type="button"
                                            onClick={(e) => { e.stopPropagation(); setDraftConfig(config); }}
                                            className="text-left rounded-sm"
                                        >
                                            {config.name}
                                        </button>
                                        <span className={`text-[10px] font-bold px-2 py-0.5 border uppercase tracking-wide ${getStatusColor(config.status)}`}>
                                            {config.status}
                                        </span>
                                        <span className="text-[10px] font-bold px-2 py-0.5 border border-zinc-700 text-text-faint uppercase">
                                            {config.db_type}
                                        </span>
                                    </h4>
                                    <p className="text-xs text-text-faint font-mono truncate max-w-lg mt-1">
                                        {config.connection_string.replace(/:\/\/[^@]+@/, '://***@')}
                                    </p>
                                </div>
                                <div className="flex items-center gap-2 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity">
                                    <button
                                        onClick={(e) => handleRefreshSchema(config.id, e)}
                                        aria-label={`Refresh the schema of ${config.name}`}
                                        className="p-2 text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors"
                                        title="Refresh Schema"
                                        disabled={isRefreshing}
                                    >
                                        <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin text-white' : ''}`} />
                                    </button>
                                    <button
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            setConfirmDeleteId(config.id);
                                        }}
                                        className="p-2 text-zinc-400 hover:text-red-500 hover:bg-red-500/10 transition-colors"
                                        aria-label={`Delete ${config.name}`}
                                        title="Delete"
                                    >
                                        <Trash2 className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>
                            {config.description && (
                                <p className="text-sm text-zinc-400 mt-3 line-clamp-2">{config.description}</p>
                            )}
                            {config.error_message && (
                                <p className="text-xs text-red-400 mt-2 font-mono">{config.error_message}</p>
                            )}
                            <div className="flex gap-4 mt-4 text-xs font-bold text-text-faint uppercase tracking-wider">
                                {config.schema_info
                                    ? <span>{config.schema_info.split('\n').length - 1} TABLES CACHED</span>
                                    : <span>SCHEMA NOT FETCHED — hover and click refresh</span>
                                }
                                {config.last_tested && (
                                    <span>• TESTED {new Date(config.last_tested).toLocaleString()}</span>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            )}

            <ConfirmationModal
                isOpen={!!confirmDeleteId}
                title="Delete Database Config"
                message="Are you sure you want to delete this database connection? This action cannot be undone."
                onConfirm={() => {
                    if (confirmDeleteId) handleDeleteConfig(confirmDeleteId);
                    setConfirmDeleteId(null);
                }}
                onClose={() => setConfirmDeleteId(null)}
            />
        </div>
    );
}
