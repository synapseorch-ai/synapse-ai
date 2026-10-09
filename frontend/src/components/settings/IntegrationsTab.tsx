/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback } from 'react';
import { SavedSecret, secretPlaceholder, useSavedSecrets } from './SavedSecret';
import { Shield, CheckCircle2, XCircle, RefreshCw, ChevronDown, ChevronUp, User, ExternalLink } from 'lucide-react';
import { IconButton, Input, Section, Textarea, buttonStyles } from '@/components/ui';
import { FIELD_LABEL, FORM } from './form';
import { SaveBar } from './SaveBar';

/** A state as a dot and a word (UX1): success is quiet, never a filled chip. */
const Status = ({ on, children }: { on: boolean; children: React.ReactNode }) => (
    <span className="inline-flex items-center gap-1.5 text-xs text-text-muted">
        <span aria-hidden className={`size-1.5 rounded-full ${on ? 'bg-success' : 'bg-text-faint'}`} />
        {children}
    </span>
);

const LINK = 'text-accent-ink underline-offset-2 hover:underline';
const pasteBorder = (s: string) => s === 'saved' ? 'border-success' : '';
const pasteTone = (s: string) =>
    s === 'saved' ? 'text-success' : s === 'error' ? 'text-danger' : s === 'saving' ? 'text-text-muted' : 'text-text-faint';

interface CredInfo {
    has_credentials: boolean;
    client_id?: string;
    project_id?: string;
    is_connected?: boolean;
    user_email?: string | null;
    error?: string;
    /** Where Google sends a person back: the backend's own address (F309). */
    redirect_uri?: string;
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
    // The key is never served back (F201): one is saved, or it is not.
    const { secrets } = useSavedSecrets();
    const n8nConfigured = !!n8nApiKey || !!secrets.n8n_api_key?.set;
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
        <div>

            {/* ── Google Workspace ────────────────────────────────────── */}
            <Section
                variant="plain"
                collapsible={false}
                title="Google Workspace"
                actions={
                    <>
                        <span className="text-xs text-text-faint">Connection status</span>
                        <Status on={!!isConnected}>{isConnected ? 'Connected' : 'Disconnected'}</Status>
                        <IconButton
                            label="Refresh status"
                            icon={RefreshCw}
                            size="sm"
                            onClick={fetchCredInfo}
                            disabled={loading}
                            className={loading ? '[&>svg]:animate-spin' : ''}
                        />
                    </>
                }
            >
                <div className={`${FORM} space-y-6`}>

                    {/* ── CASE A: Credentials Exist ─── */}
                    {hasCredentials ? (
                        <div className="space-y-4">
                            {/* Credential summary: a card of its own (the one bordered object here) */}
                            <div className="space-y-3 rounded-md border border-border bg-surface p-4">
                                <div className="flex items-center gap-2">
                                    <CheckCircle2 className="size-4 text-success" aria-hidden />
                                    <span className="text-sm font-medium text-text">OAuth credentials configured</span>
                                </div>

                                <div className="grid grid-cols-2 gap-x-6 gap-y-2">
                                    <div>
                                        <p className="mb-0.5 text-xs text-text-faint">Client ID</p>
                                        <p className="break-all font-code text-xs text-text">{credInfo?.client_id ?? '—'}</p>
                                    </div>
                                    <div>
                                        <p className="mb-0.5 text-xs text-text-faint">Project</p>
                                        <p className="break-all font-code text-xs text-text">{credInfo?.project_id || '—'}</p>
                                    </div>
                                </div>

                                {/* User email or connect prompt */}
                                {isConnected && userEmail ? (
                                    <div className="flex items-center gap-2 border-t border-border pt-3">
                                        <User className="size-3.5 text-text-faint" aria-hidden />
                                        <span className="text-sm text-text">{userEmail}</span>
                                        <a
                                            href="/auth/login"
                                            target="_blank"
                                            rel="noreferrer"
                                            className={`ml-auto text-xs ${LINK}`}
                                            title="Re-authenticate with Google"
                                        >
                                            Reconnect
                                        </a>
                                        <Status on>Authenticated</Status>
                                    </div>
                                ) : isConnected ? (
                                    <div className="flex items-center gap-2 border-t border-border pt-3">
                                        <CheckCircle2 className="size-3.5 text-success" aria-hidden />
                                        <span className="text-sm text-text-muted">Token active — no email info stored</span>
                                    </div>
                                ) : (
                                    <div className="space-y-2 border-t border-border pt-3">
                                        <p className="text-sm text-text-muted">
                                            OAuth credentials are ready. Click below to authenticate your Google account.
                                        </p>
                                        <a
                                            href="/auth/login"
                                            target="_blank"
                                            rel="noreferrer"
                                            className={buttonStyles({ variant: 'primary', size: 'sm' })}
                                        >
                                            <Shield className="size-3.5" aria-hidden />
                                            Connect Google account
                                            <ExternalLink className="size-3 opacity-60" aria-hidden />
                                        </a>
                                        <p className="text-xs text-text-faint">
                                            This opens a Google sign-in page. After completing, return here and refresh.
                                        </p>
                                    </div>
                                )}
                            </div>

                            {/* Re-upload toggle */}
                            <div>
                                <button
                                    onClick={() => setShowReupload(v => !v)}
                                    className="flex items-center gap-2 rounded-sm text-sm text-text-muted transition-colors hover:text-text"
                                >
                                    {showReupload ? <ChevronUp className="size-3.5" aria-hidden /> : <ChevronDown className="size-3.5" aria-hidden />}
                                    Replace / re-upload credentials.json
                                </button>
                                {showReupload && (
                                    <div className="mt-2 space-y-1.5 animate-in fade-in slide-in-from-top-1 duration-150">
                                        <Textarea
                                            className={`h-28 resize-none font-code text-xs ${pasteBorder(pasteStatus)}`}
                                            invalid={pasteStatus === 'error'}
                                            placeholder='{"installed":{"client_id":"...","project_id":"..."}}'
                                            onChange={e => handleCredentialsPaste(e.target.value)}
                                        />
                                        <p className={`text-xs ${pasteTone(pasteStatus)}`}>
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
                                    className="rounded-sm text-sm text-text-muted underline decoration-dotted underline-offset-2 transition-colors hover:text-text"
                                >
                                    Advanced: import existing token JSON (skip OAuth)
                                </button>
                                {showTokenImport && (
                                    <div className="mt-2 animate-in fade-in slide-in-from-top-1 duration-150">
                                        <Textarea
                                            className="h-24 resize-none font-code text-xs"
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
                            <div className="flex items-start gap-3">
                                <div className="relative shrink-0">
                                    <Shield className="size-6 text-text-faint" aria-hidden />
                                    <XCircle className="absolute -bottom-1 -right-1 size-3.5 text-text-muted" aria-hidden />
                                </div>
                                <div>
                                    <h3 className="text-sm font-medium text-text">Google Workspace setup</h3>
                                    <p className="mt-0.5 text-sm text-text-muted">
                                        Google Workspace tools are powered by the Google Workspace MCP server.
                                        Upload your OAuth credentials below to enable it.
                                    </p>
                                </div>
                            </div>

                            <div className="max-h-[300px] overflow-y-auto rounded-md border border-border bg-surface p-4 text-sm">
                                <p className="mb-2 font-medium text-text">Quick setup guide:</p>
                                <ol className="list-decimal space-y-1.5 pl-5 text-text-muted">
                                    <li>Go to <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer" className={LINK}>Google Cloud Console</a> &amp; create a Project.</li>
                                    <li>Enable <em>Gmail, Drive, Calendar, Docs, Sheets, Slides, Forms, Tasks, People APIs</em> — <a href="https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com,drive.googleapis.com,calendar-json.googleapis.com,docs.googleapis.com,sheets.googleapis.com,slides.googleapis.com,forms.googleapis.com,tasks.googleapis.com,people.googleapis.com" target="_blank" rel="noreferrer" className={LINK}>one-click enable APIs →</a></li>
                                    <li>Create OAuth Client ID — use this direct link: <a href="https://console.cloud.google.com/auth/clients/create" target="_blank" rel="noreferrer" className={LINK}>Create OAuth Client →</a> and choose <strong className="font-medium text-text">Web application</strong>.</li>
                                    <li>Under <strong className="font-medium text-text">Authorized redirect URIs</strong>, add <code className="break-all rounded bg-surface-2 px-1 font-code text-xs text-text">{credInfo?.redirect_uri ?? 'http://localhost:8765/auth/callback'}</code></li>
                                    <li>Download the JSON file, open it, copy all its content, and paste it below.</li>
                                </ol>
                            </div>

                            <div className="space-y-4">
                                {/* Paste area */}
                                <details className="group appearance-none">
                                    <summary className="flex cursor-pointer list-none items-center gap-2 rounded-md border border-border px-3 py-2.5 text-sm font-medium text-text-muted transition-colors hover:border-border-strong hover:text-text">
                                        <span>+ Paste credentials.json content</span>
                                    </summary>
                                    <div className="mt-2 space-y-1.5">
                                        <Textarea
                                            className={`h-32 resize-none font-code text-xs ${pasteBorder(pasteStatus)}`}
                                            invalid={pasteStatus === 'error'}
                                            placeholder='{"installed":{"client_id":"...","project_id":"..."}}'
                                            onChange={e => handleCredentialsPaste(e.target.value)}
                                        />
                                        <p className={`text-xs ${pasteTone(pasteStatus)}`}>
                                            {pasteStatus === 'saved' ? '✓ Saved — refreshing status…' :
                                                pasteStatus === 'error' ? '✗ Invalid JSON or save failed.' :
                                                    pasteStatus === 'saving' ? 'Saving…' :
                                                        'Paste valid JSON and it saves automatically.'}
                                        </p>
                                    </div>
                                </details>

                                {/* Token Import */}
                                <details className="group appearance-none">
                                    <summary className="flex cursor-pointer list-none items-center gap-2 text-sm text-text-muted hover:text-text">
                                        <span className="underline decoration-dotted underline-offset-2">Advanced: import existing token JSON (skip OAuth)</span>
                                    </summary>
                                    <div className="mt-2">
                                        <Textarea
                                            className="h-24 resize-none font-code text-xs"
                                            placeholder='{"token": "...", "refresh_token": "..."}'
                                            onChange={e => handleTokenPaste(e.target.value)}
                                        />
                                    </div>
                                </details>
                            </div>
                        </div>
                    )}
                </div>
            </Section>

            {/* ── n8n ─────────────────────────────────────────────────── */}
            <Section
                variant="plain"
                collapsible={false}
                title="n8n"
                actions={<Status on={n8nConfigured}>{n8nConfigured ? 'Configured' : 'Not configured'}</Status>}
            >
                <div className={`${FORM} space-y-6`}>
                    <div className="space-y-1.5">
                        <label htmlFor="integrations-n8n-url" className={FIELD_LABEL}>n8n URL</label>
                        <Input
                            id="integrations-n8n-url"
                            type="text"
                            value={n8nUrl}
                            onChange={(e) => setN8nUrl(e.target.value)}
                            className="font-code"
                            placeholder="http://localhost:5678"
                        />
                        <p className="text-xs text-text-faint">Defaults to localhost for local dev. Use your production n8n base URL in deployment.</p>
                    </div>
                    <div className="space-y-1.5">
                        <label htmlFor="integrations-n8n-api-key" className={FIELD_LABEL}>n8n API key</label>
                        <Input
                            id="integrations-n8n-api-key"
                            type="password"
                            value={n8nApiKey}
                            onChange={(e) => setN8nApiKey(e.target.value)}
                            className="font-code"
                            placeholder={secretPlaceholder(secrets, 'n8n_api_key', 'X-N8N-API-KEY')}
                        />
                        <SavedSecret name="n8n_api_key" label="n8n API key" typed={n8nApiKey} />
                        <p className="text-xs text-text-faint">Used server-side to list workflows and derive webhook URLs.</p>
                    </div>

                    <SaveBar onSave={onSave} />
                </div>
            </Section>

        </div>
    );
};
