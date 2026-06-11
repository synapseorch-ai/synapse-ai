/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback } from 'react';
import { Shield, CheckCircle2, XCircle, RefreshCw, ChevronDown, ChevronUp, User, ExternalLink } from 'lucide-react';

interface CredInfo {
    has_credentials: boolean;
    client_id?: string;
    project_id?: string;
    is_connected?: boolean;
    user_email?: string | null;
    error?: string;
}

interface IntegrationsTabProps {
    n8nUrl: string; setN8nUrl: (v: string) => void;
    n8nApiKey: string; setN8nApiKey: (v: string) => void;
    onSave: () => void;
}

export const IntegrationsTab = ({
    n8nUrl, setN8nUrl, n8nApiKey, setN8nApiKey,
    onSave
}: IntegrationsTabProps) => {
    const [credInfo, setCredInfo] = useState<CredInfo | null>(null);
    const [loading, setLoading] = useState(true);
    const [showReupload, setShowReupload] = useState(false);
    const [showTokenImport, setShowTokenImport] = useState(false);
    const [pasteStatus, setPasteStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');

    const fetchCredInfo = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch('/api/config');
            const data = await res.json();
            setCredInfo(data);
        } catch {
            setCredInfo({ has_credentials: false, is_connected: false });
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { fetchCredInfo(); }, [fetchCredInfo]);

    // Auto-refresh when returning from Google OAuth (backend redirects with ?google_auth=success)
    useEffect(() => {
        const params = new URLSearchParams(window.location.search);
        const authResult = params.get('google_auth');
        if (authResult === 'success') {
            fetchCredInfo();
            // Clean the query param from the URL
            window.history.replaceState({}, '', window.location.pathname);
        } else if (authResult === 'error') {
            const reason = params.get('reason') || 'Unknown error';
            console.error('Google OAuth error:', reason);
            window.history.replaceState({}, '', window.location.pathname);
        }
    }, [fetchCredInfo]);

    const handleCredentialsPaste = async (val: string) => {
        try {
            JSON.parse(val);
        } catch {
            return; // not valid JSON yet, wait
        }
        setPasteStatus('saving');
        try {
            const res = await fetch('/api/setup/google-credentials', {
                method: 'POST',
                body: val,
                headers: { 'Content-Type': 'application/json' }
            });
            if (res.ok) {
                setPasteStatus('saved');
                setTimeout(async () => {
                    await fetchCredInfo();
                    setPasteStatus('idle');
                    setShowReupload(false);
                }, 1200);
            } else {
                setPasteStatus('error');
                setTimeout(() => setPasteStatus('idle'), 2000);
            }
        } catch {
            setPasteStatus('error');
            setTimeout(() => setPasteStatus('idle'), 2000);
        }
    };

    const handleTokenPaste = async (val: string) => {
        try {
            JSON.parse(val);
        } catch {
            return;
        }
        try {
            const res = await fetch('/api/setup/google-token', {
                method: 'POST',
                body: val,
                headers: { 'Content-Type': 'application/json' }
            });
            if (res.ok) {
                await fetchCredInfo();
                setShowTokenImport(false);
            }
        } catch { /* ignore */ }
    };

    const hasCredentials = credInfo?.has_credentials;
    const isConnected = credInfo?.is_connected;
    const userEmail = credInfo?.user_email;

    return (
        <div className="space-y-8">

            {/* ── Google Workspace ────────────────────────────────────── */}
            <div className="bg-zinc-900 border border-zinc-800 overflow-hidden">

                {/* Header */}
                <div className="p-4 border-b border-zinc-800 bg-zinc-950 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <div className={`h-2 w-2 transition-colors ${isConnected ? 'bg-green-500 shadow-[0_0_10px_rgba(34,197,94,0.5)]' : 'bg-red-500'}`} />
                        <span className="text-sm font-bold text-zinc-400">Connection Status</span>
                    </div>
                    <div className="flex items-center gap-2">
                        <button
                            onClick={fetchCredInfo}
                            disabled={loading}
                            className="p-1 text-zinc-600 hover:text-zinc-400 transition-colors"
                            title="Refresh status"
                        >
                            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
                        </button>
                        <span className={`text-xs px-2 py-1 bg-zinc-900 border border-zinc-800 ${isConnected ? 'text-green-400' : 'text-zinc-500'}`}>
                            {isConnected ? 'CONNECTED' : 'DISCONNECTED'}
                        </span>
                    </div>
                </div>

                <div className="p-6 space-y-6">

                    {/* ── CASE A: Credentials Exist ─── */}
                    {hasCredentials ? (
                        <div className="space-y-4">
                            {/* Credential Summary Card */}
                            <div className="bg-zinc-950 border border-zinc-800 p-4 space-y-3">
                                <div className="flex items-center gap-2 mb-3">
                                    <CheckCircle2 className="h-4 w-4 text-green-500" />
                                    <span className="text-xs font-bold text-green-400 uppercase tracking-wider">OAuth Credentials Configured</span>
                                </div>

                                <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-[11px]">
                                    <div>
                                        <p className="text-zinc-600 uppercase tracking-wider mb-0.5">Client ID</p>
                                        <p className="font-mono text-zinc-300">{credInfo?.client_id ?? '—'}</p>
                                    </div>
                                    <div>
                                        <p className="text-zinc-600 uppercase tracking-wider mb-0.5">Project</p>
                                        <p className="font-mono text-zinc-300">{credInfo?.project_id || '—'}</p>
                                    </div>
                                </div>

                                {/* User email or connect prompt */}
                                {isConnected && userEmail ? (
                                    <div className="mt-2 pt-2 border-t border-zinc-800 flex items-center gap-2">
                                        <User className="h-3.5 w-3.5 text-green-500" />
                                        <span className="text-xs text-zinc-300">{userEmail}</span>
                                        <a
                                            href="/auth/login"
                                            target="_blank"
                                            rel="noreferrer"
                                            className="ml-auto text-[10px] text-zinc-500 hover:text-zinc-300 underline decoration-dotted"
                                            title="Re-authenticate with Google"
                                        >
                                            Reconnect
                                        </a>
                                        <span className="text-[10px] text-green-500 uppercase tracking-wider">Authenticated</span>
                                    </div>
                                ) : isConnected ? (
                                    <div className="mt-2 pt-2 border-t border-zinc-800 flex items-center gap-2">
                                        <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
                                        <span className="text-xs text-zinc-400">Token active — no email info stored</span>
                                    </div>
                                ) : (
                                    <div className="mt-3 pt-3 border-t border-zinc-800 space-y-2">
                                        <p className="text-[11px] text-zinc-500">
                                            OAuth credentials are ready. Click below to authenticate your Google account.
                                        </p>
                                        <a
                                            href="/auth/login"
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-2 px-4 py-2 bg-white text-black text-xs font-bold uppercase tracking-wider hover:bg-zinc-200 transition-colors"
                                        >
                                            <Shield className="h-3.5 w-3.5" />
                                            Connect Google Account
                                            <ExternalLink className="h-3 w-3 opacity-60" />
                                        </a>
                                        <p className="text-[10px] text-zinc-600">
                                            This opens a Google sign-in page. After completing, return here and refresh.
                                        </p>
                                    </div>
                                )}
                            </div>

                            {/* Re-upload toggle */}
                            <div>
                                <button
                                    onClick={() => setShowReupload(v => !v)}
                                    className="flex items-center gap-2 text-[11px] text-zinc-500 hover:text-zinc-300 transition-colors"
                                >
                                    {showReupload ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                                    Replace / Re-upload credentials.json
                                </button>
                                {showReupload && (
                                    <div className="mt-2 space-y-2 animate-in fade-in slide-in-from-top-1 duration-150">
                                        <textarea
                                            className={`w-full h-28 bg-black border p-3 text-[10px] font-mono text-zinc-300 focus:outline-none resize-none transition-colors ${pasteStatus === 'saved' ? 'border-green-600' :
                                                    pasteStatus === 'error' ? 'border-red-600' : 'border-zinc-800 focus:border-white'
                                                }`}
                                            placeholder='{"installed":{"client_id":"...","project_id":"..."}}'
                                            onChange={e => handleCredentialsPaste(e.target.value)}
                                        />
                                        <p className={`text-[10px] ${pasteStatus === 'saved' ? 'text-green-500' :
                                                pasteStatus === 'error' ? 'text-red-500' :
                                                    pasteStatus === 'saving' ? 'text-zinc-400' : 'text-zinc-700'
                                            }`}>
                                            {pasteStatus === 'saved' ? '✓ Credentials saved — refreshing…' :
                                                pasteStatus === 'error' ? '✗ Failed to save credentials.' :
                                                    pasteStatus === 'saving' ? 'Saving…' :
                                                        'Paste valid JSON and it saves automatically.'}
                                        </p>
                                    </div>
                                )}
                            </div>

                            {/* Advanced token import */}
                            <div>
                                <button
                                    onClick={() => setShowTokenImport(v => !v)}
                                    className="text-[11px] text-zinc-600 hover:text-zinc-400 underline decoration-dotted transition-colors"
                                >
                                    Advanced: Import existing Token JSON (Skip OAuth)
                                </button>
                                {showTokenImport && (
                                    <div className="mt-2 animate-in fade-in slide-in-from-top-1 duration-150">
                                        <textarea
                                            className="w-full h-24 bg-black border border-zinc-800 p-3 font-mono text-xs text-zinc-300 focus:border-white focus:outline-none resize-none"
                                            placeholder='{"token": "...", "refresh_token": "..."}'
                                            onChange={e => handleTokenPaste(e.target.value)}
                                        />
                                    </div>
                                )}
                            </div>
                        </div>
                    ) : (

                        /* ── CASE B: No credentials ─── */
                        <div className="space-y-5">
                            <div className="text-center py-4 px-4">
                                <div className="relative inline-block mb-3">
                                    <Shield className="h-8 w-8 text-zinc-700 mx-auto" />
                                    <XCircle className="h-4 w-4 text-red-500 absolute -bottom-1 -right-1" />
                                </div>
                                <h3 className="text-base text-white font-bold mb-1">Google Workspace Setup</h3>
                                <p className="text-xs text-zinc-500 max-w-sm mx-auto mb-4">
                                    Google Workspace tools are powered by the Google Workspace MCP server.
                                    Upload your OAuth credentials below to enable it.
                                </p>

                                <div className="text-left text-[10px] bg-black p-4 border border-zinc-800 mb-5 max-w-md mx-auto overflow-y-auto max-h-[300px]">
                                    <p className="font-bold text-zinc-300 mb-2">Quick Setup Guide:</p>
                                    <ol className="list-decimal pl-4 space-y-1.5 text-zinc-500">
                                        <li>Go to <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer" className="text-blue-400 hover:text-blue-300 underline">Google Cloud Console</a> &amp; create a Project.</li>
                                        <li>Enable <em>Gmail, Drive, Calendar, Docs, Sheets, Slides, Forms, Tasks, People APIs</em> — <a href="https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com,drive.googleapis.com,calendar-json.googleapis.com,docs.googleapis.com,sheets.googleapis.com,slides.googleapis.com,forms.googleapis.com,tasks.googleapis.com,people.googleapis.com" target="_blank" rel="noreferrer" className="text-blue-400 hover:text-blue-300 underline">one-click enable APIs →</a></li>
                                        <li>Create OAuth Client ID — use this direct link: <a href="https://console.cloud.google.com/auth/clients/create" target="_blank" rel="noreferrer" className="text-blue-400 hover:text-blue-300 underline">Create OAuth Client →</a> and choose <strong className="text-zinc-300">Web application</strong>.</li>
                                        <li>Under <strong>Authorized redirect URIs</strong>, add <strong className="text-zinc-300">http://localhost:{process.env.NEXT_PUBLIC_BACKEND_PORT || '8765'}/auth/callback</strong></li>
                                        <li>Download the JSON file, open it, copy all its content, and paste it below.</li>
                                    </ol>
                                </div>
                            </div>

                            <div className="border-t border-zinc-800 pt-5 space-y-4">
                                {/* Paste area */}
                                <details className="group appearance-none">
                                    <summary className="cursor-pointer text-xs font-bold text-zinc-400 hover:text-white list-none flex items-center gap-2 p-3 bg-zinc-950 border border-zinc-800 hover:border-zinc-700 transition-colors">
                                        <span>+ PASTE CREDENTIALS.JSON CONTENT</span>
                                    </summary>
                                    <div className="mt-2">
                                        <textarea
                                            className={`w-full h-32 bg-black border p-3 text-[10px] font-mono text-zinc-300 focus:outline-none resize-none transition-colors ${pasteStatus === 'saved' ? 'border-green-600' :
                                                    pasteStatus === 'error' ? 'border-red-600' : 'border-zinc-800 focus:border-white'
                                                }`}
                                            placeholder='{"installed":{"client_id":"...","project_id":"..."}}'
                                            onChange={e => handleCredentialsPaste(e.target.value)}
                                        />
                                        <p className={`mt-1 text-[10px] ${pasteStatus === 'saved' ? 'text-green-500' :
                                                pasteStatus === 'error' ? 'text-red-500' :
                                                    pasteStatus === 'saving' ? 'text-zinc-400' : 'text-zinc-700'
                                            }`}>
                                            {pasteStatus === 'saved' ? '✓ Saved — refreshing status…' :
                                                pasteStatus === 'error' ? '✗ Invalid JSON or save failed.' :
                                                    pasteStatus === 'saving' ? 'Saving…' :
                                                        'Paste valid JSON and it saves automatically.'}
                                        </p>
                                    </div>
                                </details>

                                {/* Token Import */}
                                <details className="group appearance-none">
                                    <summary className="cursor-pointer text-xs font-bold text-zinc-500 hover:text-white mt-2 list-none flex items-center gap-2">
                                        <span className="underline decoration-dotted decoration-zinc-700 hover:decoration-zinc-500">Advanced: Import existing Token JSON (Skip OAuth)</span>
                                    </summary>
                                    <div className="mt-2">
                                        <textarea
                                            className="w-full h-24 bg-black border border-zinc-800 p-3 font-mono text-xs text-zinc-300 focus:border-white focus:outline-none resize-none"
                                            placeholder='{"token": "...", "refresh_token": "..."}'
                                            onChange={e => handleTokenPaste(e.target.value)}
                                        />
                                    </div>
                                </details>
                            </div>
                        </div>
                    )}
                </div>
            </div>

            {/* ── n8n ─────────────────────────────────────────────────── */}
            <div className="bg-zinc-900 border border-zinc-800 overflow-hidden">
                <div className="p-4 border-b border-zinc-800 bg-zinc-950 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <div className={`h-2 w-2 ${n8nApiKey ? 'bg-green-500' : 'bg-red-500'}`} />
                        <span className="text-sm font-bold text-zinc-400">n8n</span>
                    </div>
                    <span className={`text-xs px-2 py-1 bg-zinc-900 border border-zinc-800 ${n8nApiKey ? 'text-green-400' : 'text-zinc-500'}`}>
                        {n8nApiKey ? 'CONFIGURED' : 'NOT CONFIGURED'}
                    </span>
                </div>
                <div className="p-6 space-y-6">
                    <div className="space-y-2">
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">n8n URL</label>
                        <input
                            type="text"
                            value={n8nUrl}
                            onChange={(e) => setN8nUrl(e.target.value)}
                            className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono"
                            placeholder="http://localhost:5678"
                        />
                        <p className="text-xs text-zinc-600">Defaults to localhost for local dev. Use your production n8n base URL in deployment.</p>
                    </div>
                    <div className="space-y-2">
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">n8n API Key</label>
                        <input
                            type="password"
                            value={n8nApiKey}
                            onChange={(e) => setN8nApiKey(e.target.value)}
                            className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono"
                            placeholder="X-N8N-API-KEY"
                        />
                        <p className="text-xs text-zinc-600">Used server-side to list workflows and derive webhook URLs.</p>
                    </div>

                    <div className="pt-2 flex justify-end">
                        <button
                            onClick={onSave}
                            className="px-6 py-2.5 text-sm font-bold bg-white text-black hover:bg-zinc-200 transition-all shadow-lg"
                        >
                            Save Changes
                        </button>
                    </div>
                </div>
            </div>

        </div>
    );
};
