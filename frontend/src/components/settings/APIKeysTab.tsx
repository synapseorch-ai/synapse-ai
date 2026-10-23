"use client";
/*
 * Settings › API keys: the keys and where to call (F906). Naveen: "under api lets not have a documentation ...
 * lets have a link to documentation". The page kept a V1/V2 reference drawer of its own; the docs site has it all
 * (DOC5: /api/overview, /api/mcp-server, /api/chat, /api/orchestrations and a page per endpoint), so the page links
 * there and keeps what it is for: making and removing keys, and the base URL a client calls.
 */
import { useState, useEffect, useCallback, useSyncExternalStore } from 'react';
import { Copy, CheckCircle, AlertCircle, ExternalLink } from 'lucide-react';
import { Button, CopyButton, EmptyState, Input, LinkButton, Modal, Section, TextLink } from '@/components/ui';
import { DOCS_BASE } from '@/components/help';
import { FORM } from './form';

interface ApiKeyRecord {
    id: string;
    name: string;
    key_prefix: string;
    created_at: string;
    last_used_at: string | null;
    is_active: boolean;
}

// The origin this page was served from, which is the API's origin too: the UI
// rewrites `/api/v1` and `/api/v2` to the backend, so the address in the
// browser's bar is the one a client should call — `localhost:3000` on a laptop,
// the install's own host behind a proxy. It was `http://localhost:{backend
// port}`, which is wrong everywhere but the machine itself (Docker binds the
// backend to 127.0.0.1) and wrong there for anyone who changed the port.
// Empty during the server render.
const subscribeNever = () => () => {};
const useOrigin = () => useSyncExternalStore(subscribeNever, () => window.location.origin, () => '');

// ── Main Tab ─────────────────────────────────────────────────────────────────

export const APIKeysTab = () => {
    const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
    const [loading, setLoading] = useState(true);
    const [newKeyName, setNewKeyName] = useState('');
    const [generating, setGenerating] = useState(false);
    const [revealedKey, setRevealedKey] = useState<string | null>(null);
    const [copied, setCopied] = useState(false);
    const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
    const origin = useOrigin();

    const showToast = (message: string, type: 'success' | 'error' = 'success') => {
        setToast({ message, type });
        setTimeout(() => setToast(null), 4000);
    };

    const fetchKeys = useCallback(async () => {
        try {
            const res = await fetch('/api/settings/api-keys');
            if (res.ok) setKeys(await res.json());
        } catch { /* ignore */ }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { fetchKeys(); }, [fetchKeys]);

    const handleGenerate = async () => {
        if (!newKeyName.trim()) { showToast('Please enter a key name', 'error'); return; }
        setGenerating(true);
        try {
            const res = await fetch('/api/settings/api-keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newKeyName.trim() }),
            });
            if (res.ok) {
                const data = await res.json();
                setRevealedKey(data.key);
                setNewKeyName('');
                fetchKeys();
                showToast('API key generated!', 'success');
            } else {
                showToast('Failed to generate key', 'error');
            }
        } catch { showToast('Error generating key', 'error'); }
        finally { setGenerating(false); }
    };

    const handleDelete = async (id: string) => {
        try {
            const res = await fetch(`/api/settings/api-keys/${id}`, { method: 'DELETE' });
            if (res.ok) { setKeys(prev => prev.filter(k => k.id !== id)); showToast('Key deleted', 'success'); }
        } catch { showToast('Error deleting key', 'error'); }
    };

    const handleCopy = async (text: string) => {
        await navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };

    return (
        <div className="space-y-8">
            {/* Toast */}
            {toast && (
                <div className="fixed top-6 right-6 z-50 flex items-center gap-2 rounded-md border border-border bg-surface px-4 py-3 text-sm font-medium text-text shadow-lg">
                    {toast.type === 'success' ? <CheckCircle className="w-4 h-4 text-success" /> : <AlertCircle className="w-4 h-4 text-danger" />}
                    {toast.message}
                </div>
            )}

            {/* Key Reveal Modal */}
            {revealedKey && (
                <Modal
                    open
                    onClose={() => setRevealedKey(null)}
                    title="Your API key"
                    description={<>Copy it now — it will <strong className="text-text">never be shown again</strong>.</>}
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setRevealedKey(null)}>Done</Button>
                            <Button onClick={() => handleCopy(revealedKey)}>
                                {copied ? <CheckCircle className="size-3.5" /> : <Copy className="size-3.5" />}
                                {copied ? 'Copied' : 'Copy key'}
                            </Button>
                        </>
                    }
                >
                    <div className="border border-border bg-bg p-3 font-code text-sm break-all text-success rounded-md">
                        {revealedKey}
                    </div>
                </Modal>
            )}

            {/* Generate New Key */}
            <Section
                variant="plain"
                collapsible={false}
                title="Generate API key"
                actions={
                    <LinkButton href={`${DOCS_BASE}/api/overview`} external variant="secondary" size="sm" target="_blank" rel="noopener noreferrer">
                        API reference
                        <ExternalLink className="size-3.5" aria-hidden />
                        <span className="sr-only"> (opens in a new tab)</span>
                    </LinkButton>
                }
            >
                <div className={`${FORM} space-y-3`}>
                    <p className="text-sm text-text-muted">
                        API keys authenticate calls to <code className="rounded bg-surface-2 px-1 font-code text-xs text-text">/api/v1/*</code> (synchronous) and <code className="rounded bg-surface-2 px-1 font-code text-xs text-text">/api/v2/*</code> (distributed, scale mode).
                    </p>
                    <div className="flex gap-2">
                        <Input
                            type="text"
                            value={newKeyName}
                            onChange={e => setNewKeyName(e.target.value)}
                            onKeyDown={e => e.key === 'Enter' && handleGenerate()}
                            placeholder="Key name (e.g., Slack Bot, Internal Tool)"
                            aria-label="New API key name"
                            className="flex-1"
                        />
                        <Button onClick={handleGenerate} disabled={generating}>
                            {generating ? 'Generating…' : 'Generate'}
                        </Button>
                    </div>
                </div>
            </Section>

            {/* Where a client calls: the address in the browser's bar is the API's too. */}
            <Section variant="plain" collapsible={false} title="Base URL">
                <div className={`${FORM} space-y-3`}>
                    {(['v1', 'v2'] as const).map(version => (
                        <div key={version} className="flex items-center gap-2">
                            <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-surface-2 px-3 py-2 font-code text-xs text-text" translate="no">
                                {origin}/api/{version}
                            </code>
                            <CopyButton text={() => `${origin}/api/${version}`} label={`Copy ${version}`} />
                        </div>
                    ))}
                    <p className="text-sm text-text-muted">
                        Send a key as <code className="rounded bg-surface-2 px-1 font-code text-xs text-text">Authorization: Bearer &lt;key&gt;</code>.
                        Every endpoint, with examples, is in the{' '}
                        <TextLink external href={`${DOCS_BASE}/api/overview`} target="_blank" rel="noopener noreferrer">API reference</TextLink>;
                        connecting Claude Code or Cursor is in{' '}
                        <TextLink external href={`${DOCS_BASE}/api/mcp-server`} target="_blank" rel="noopener noreferrer">the MCP server guide</TextLink>.
                    </p>
                </div>
            </Section>

            {/* Keys List */}
            <Section variant="plain" collapsible={false} title="Active keys">
                {loading ? (
                    <p className="text-sm text-text-faint py-4">Loading…</p>
                ) : keys.length === 0 ? (
                    <EmptyState title="No API keys yet.">Generate one above to get started.</EmptyState>
                ) : (
                    <div className="divide-y divide-border rounded-md border border-border">
                        {keys.map(k => (
                            <div key={k.id} className="flex items-center justify-between gap-3 px-4 py-3 hover:bg-row-hover">
                                <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-3">
                                        <span className="text-sm font-medium text-text">{k.name}</span>
                                        <code className="text-xs font-code text-text-faint">
                                            {k.key_prefix}…
                                        </code>
                                    </div>
                                    <div className="flex items-center gap-4 text-xs text-text-faint mt-0.5">
                                        <span>Created: {new Date(k.created_at).toLocaleDateString()}</span>
                                        <span>Last used: {k.last_used_at ? new Date(k.last_used_at).toLocaleDateString() : 'Never'}</span>
                                    </div>
                                </div>
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => handleDelete(k.id)}
                                    className="shrink-0 text-text-muted hover:text-danger"
                                >
                                    Remove
                                </Button>
                            </div>
                        ))}
                    </div>
                )}
            </Section>
        </div>
    );
};
