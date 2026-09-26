import { useState, useEffect } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Button, ConfirmDialog, Input, Label, Modal, Spinner } from '@/components/ui';
import { ToolPolicyFields, type ToolPolicy } from './ToolPolicy';

type EmbedIssue = 'no_psql' | 'no_db' | 'existing_url_broken' | 'no_pgvector' | 'connection_error';

interface EmbedCheckState {
    issue: EmbedIssue;
    detail?: string;
}

interface DbForm {
    host: string;
    port: string;
    username: string;
    password: string;
    dbName: string;
}

interface GeneralTabProps {
    agentName: string;
    setAgentName: (v: string) => void;
    vaultEnabled: boolean;
    setVaultEnabled: (v: boolean) => void;
    vaultThreshold: number;
    setVaultThreshold: (v: number) => void;
    autoCompactEnabled: boolean;
    setAutoCompactEnabled: (v: boolean) => void;
    autoCompactThreshold: number;
    setAutoCompactThreshold: (v: number) => void;
    allowDbWrite: boolean;
    setAllowDbWrite: (v: boolean) => void;
    toolPolicyDefaults: ToolPolicy;
    setToolPolicyDefaults: (v: ToolPolicy) => void;
    embedCode: boolean;
    setEmbedCode: (v: boolean) => void;
    bashAllowedDirs: string[];
    setBashAllowedDirs: (v: string[]) => void;
    transformRuntime: 'docker' | 'host';
    setTransformRuntime: (v: 'docker' | 'host') => void;
    /** Whether login is on, as saved. Only a successful save changes it. */
    loginEnabled: boolean;
    loginUsername: string;
    setLoginUsername: (v: string) => void;
    /** Resolves true once the backend has saved it. */
    onSaveLogin: (enabled: boolean, username: string, password: string) => Promise<boolean>;
    isLoginSaving?: boolean;
    onSave: () => void;
    isSaving?: boolean;
}

export function GeneralTab({
    agentName, setAgentName,
    vaultEnabled, setVaultEnabled,
    vaultThreshold, setVaultThreshold,
    autoCompactEnabled, setAutoCompactEnabled,
    autoCompactThreshold, setAutoCompactThreshold,
    allowDbWrite, setAllowDbWrite,
    toolPolicyDefaults, setToolPolicyDefaults,
    embedCode, setEmbedCode,
    bashAllowedDirs, setBashAllowedDirs,
    transformRuntime, setTransformRuntime,
    loginEnabled,
    loginUsername, setLoginUsername,
    onSaveLogin, isLoginSaving,
    onSave, isSaving,
}: GeneralTabProps) {
    const [embedChecking, setEmbedChecking] = useState(false);
    const [newDir, setNewDir] = useState('');
    const [vaultDraft, setVaultDraft] = useState(String(vaultThreshold));
    const [compactDraft, setCompactDraft] = useState(String(autoCompactThreshold));

    // Login form local state. The switch shows what is saved, or that login
    // is being turned on while its form is open (`enabling`) — never an "off"
    // that was not sent (F222).
    const [showLoginForm, setShowLoginForm] = useState(false);
    const [enabling, setEnabling] = useState(false);
    const [confirmLoginOff, setConfirmLoginOff] = useState(false);
    const [loginPassword, setLoginPassword] = useState('');
    const [loginConfirmPassword, setLoginConfirmPassword] = useState('');
    const [loginFormError, setLoginFormError] = useState('');
    /** Back to what is saved: nothing typed here was sent. */
    const closeLoginForm = () => {
        setShowLoginForm(false);
        setEnabling(false);
        setLoginFormError('');
        setLoginPassword('');
        setLoginConfirmPassword('');
    };

    // Confirmation modal shown when user tries to flip transform runtime to "host".
    // Going back to "docker" doesn't need confirmation (safe default).
    const [showHostRuntimeModal, setShowHostRuntimeModal] = useState(false);

    useEffect(() => { setVaultDraft(String(vaultThreshold)); }, [vaultThreshold]);
    useEffect(() => { setCompactDraft(String(autoCompactThreshold)); }, [autoCompactThreshold]);
    const [embedCheckState, setEmbedCheckState] = useState<EmbedCheckState | null>(null);
    const [dbForm, setDbForm] = useState<DbForm>({ host: 'localhost', port: '5432', username: 'postgres', password: '', dbName: 'synapse' });
    const [setupInProgress, setSetupInProgress] = useState(false);
    const [setupError, setSetupError] = useState<string | null>(null);

    const runEmbedCheck = async () => {
        setEmbedChecking(true);
        setEmbedCheckState(null);
        setSetupError(null);
        try {
            const res = await fetch('/api/settings/check-embed');
            const data = await res.json();
            if (data.all_ok) {
                setEmbedCode(true);
                setEmbedCheckState(null);
            } else if (!data.psql_available) {
                setEmbedCheckState({ issue: 'no_psql' });
            } else if (data.db_url_configured && !data.db_connection_ok) {
                // A URL is saved but the connection is failing — tell the user which one
                setEmbedCheckState({ issue: 'existing_url_broken', detail: `${data.db_url_hint}: ${data.db_error || 'connection failed'}` });
            } else if (data.db_connection_ok && !data.pgvector_available) {
                setEmbedCheckState({ issue: 'no_pgvector' });
            } else {
                // psql found but no DB URL configured yet
                setEmbedCheckState({ issue: 'no_db' });
            }
        } catch (e) {
            setEmbedCheckState({ issue: 'connection_error', detail: String(e) });
        } finally {
            setEmbedChecking(false);
        }
    };

    const handleEmbedToggle = async () => {
        if (embedCode) {
            setEmbedCode(false);
            setEmbedCheckState(null);
            return;
        }
        await runEmbedCheck();
    };

    const handleSetupDb = async () => {
        setSetupInProgress(true);
        setSetupError(null);
        try {
            const res = await fetch('/api/settings/setup-embed', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    host: dbForm.host,
                    port: parseInt(dbForm.port) || 5432,
                    username: dbForm.username,
                    password: dbForm.password,
                    db_name: dbForm.dbName,
                }),
            });
            if (!res.ok) {
                const err = await res.json();
                setSetupError(err.detail || 'Setup failed');
                return;
            }
            // Re-run check — should now pass
            await runEmbedCheck();
        } catch (e) {
            setSetupError(String(e));
        } finally {
            setSetupInProgress(false);
        }
    };

    return (
        <div className="space-y-8">
            <div className="space-y-2">
                <Label htmlFor="gen-agentname" size="sm" className="block">Global Agent Name</Label>
                <Input id="gen-agentname"
                    type="text"
                    value={agentName}
                    onChange={(e) => setAgentName(e.target.value)}
                    className="font-medium"
                    placeholder="Enter Agent Name"
                />
                <p className="text-xs text-text-faint">This name identifies your agent across the system.</p>
            </div>

            <div className="space-y-4">
                <Label size="sm" className="block">Large Response Handling</Label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-text-faint mt-0.5">When enabled, tool outputs exceeding the threshold are saved to a vault file instead of flooding the context.</p>
                    </div>
                    <button
                        role="switch"
                        aria-checked={vaultEnabled}
                        aria-label="Large response handling"
                        onClick={() => setVaultEnabled(!vaultEnabled)}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none ${vaultEnabled ? 'bg-accent' : 'bg-surface-2'}`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full transition-transform ${vaultEnabled ? 'translate-x-6 bg-accent-fg' : 'translate-x-1 bg-text-muted'}`}
                        />
                    </button>
                </div>
                {vaultEnabled && (
                    <div className="space-y-2">
                        <Label htmlFor="gen-character-threshold" size="sm" className="block">Character Threshold</Label>
                        <p className="text-xs text-text-faint">
                            ≈ <span className="text-text font-semibold">{Math.round(vaultThreshold / 4).toLocaleString()}</span> tokens
                            <span className="text-text-faint ml-1">(at ~4 chars / token)</span>
                        </p>
                        <Input id="gen-character-threshold"
                            type="number"
                            value={vaultDraft}
                            onChange={(e) => setVaultDraft(e.target.value)}
                            onBlur={() => {
                                const v = Math.max(1, parseInt(vaultDraft) || 1);
                                setVaultThreshold(v);
                                setVaultDraft(String(v));
                            }}
                            className="font-medium"
                            min={1}
                        />
                        <p className="text-xs text-text-faint">Responses longer than this many characters will be saved to a file.</p>
                    </div>
                )}
            </div>

            <div className="space-y-4">
                <Label size="sm" className="block">Auto Context Compaction</Label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-text-faint mt-0.5">
                            When the accumulated context exceeds the threshold, the agent summarises everything so far to ~30% of its size and archives the original to the vault so nothing is lost.
                        </p>
                    </div>
                    <button
                        role="switch"
                        aria-checked={autoCompactEnabled}
                        aria-label="Auto context compaction"
                        onClick={() => setAutoCompactEnabled(!autoCompactEnabled)}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 ${autoCompactEnabled ? 'bg-accent' : 'bg-surface-2'}`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full transition-transform ${autoCompactEnabled ? 'translate-x-6 bg-accent-fg' : 'translate-x-1 bg-text-muted'}`}
                        />
                    </button>
                </div>
                {autoCompactEnabled && (
                    <div className="space-y-2">
                        <Label htmlFor="gen-compaction-threshold-characters" size="sm" className="block">Compaction Threshold (characters)</Label>
                        <p className="text-xs text-text-faint">
                            ≈ <span className="text-text font-semibold">{Math.round(autoCompactThreshold / 4).toLocaleString()}</span> tokens
                            <span className="text-text-faint ml-1">(at ~4 chars / token)</span>
                        </p>
                        <Input id="gen-compaction-threshold-characters"
                            type="number"
                            value={compactDraft}
                            onChange={(e) => setCompactDraft(e.target.value)}
                            onBlur={() => {
                                const v = Math.max(10000, parseInt(compactDraft) || 10000);
                                setAutoCompactThreshold(v);
                                setCompactDraft(String(v));
                            }}
                            className="font-medium"
                            min={10000}
                        />
                        <p className="text-xs text-text-faint">
                            When context exceeds this, it is compacted using the current model. The full original is archived to the vault.
                        </p>
                    </div>
                )}
            </div>

            <div className="space-y-4">
                <Label size="sm" className="block">Database Write Access</Label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-text-faint mt-0.5">
                            When disabled (default), agents are strictly limited to SELECT/SHOW/DESCRIBE queries.
                            When enabled, INSERT/UPDATE/DELETE and other write queries are allowed — but agents must always ask for confirmation before executing them.
                        </p>
                    </div>
                    <button
                        role="switch"
                        aria-checked={allowDbWrite}
                        aria-label="Database write access"
                        onClick={() => setAllowDbWrite(!allowDbWrite)}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 ${allowDbWrite ? 'bg-warning' : 'bg-surface-2'}`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full transition-transform ${allowDbWrite ? 'translate-x-6 bg-accent-fg' : 'translate-x-1 bg-text-muted'}`}
                        />
                    </button>
                </div>
                {allowDbWrite && (
                    <div className="p-3 bg-warning/10 border border-warning/20 text-warning text-xs rounded-md">
                        <strong>Write mode active.</strong> Agents MUST ask for explicit user confirmation before running any INSERT, UPDATE, DELETE, DROP, or CREATE queries. This is enforced in the system prompt.
                    </div>
                )}
            </div>

            {/* Tool policy — the workspace floor. An agent sets its own per
                category and inherits the rest from here. */}
            <div className="space-y-4">
                <Label size="sm" className="block">Tool Policy</Label>
                <p className="text-xs text-text-faint -mt-2">
                    What every agent may do with the tools it holds, decided by what a tool does rather than
                    by its name. An agent can be stricter or looser per category; anything it leaves alone
                    follows this. Allow everywhere is the shipped default, so nothing changes until you
                    change it here.
                </p>
                <ToolPolicyFields
                    value={toolPolicyDefaults}
                    onChange={setToolPolicyDefaults}
                />
            </div>

            {/* Code Repository Indexing */}
            <div className="space-y-4">
                <Label size="sm" className="block">Code Repository Indexing</Label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-text-faint mt-0.5">
                            When enabled, agents can semantically search your indexed code repositories using vector embeddings.
                            Requires PostgreSQL with the pgvector extension.
                        </p>
                    </div>
                    <button
                        role="switch"
                        aria-checked={embedCode}
                        aria-label="Code repository indexing"
                        onClick={handleEmbedToggle}
                        disabled={embedChecking}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 ${embedCode ? 'bg-accent' : 'bg-surface-2'} ${embedChecking ? 'opacity-50 cursor-not-allowed' : ''}`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full transition-transform ${embedCode ? 'translate-x-6 bg-accent-fg' : 'translate-x-1 bg-text-muted'}`}
                        />
                    </button>
                </div>

                {/* Checking state */}
                {embedChecking && (
                    <div className="flex items-center gap-2 text-xs text-text-muted">
                        <Spinner size={14} />
                        Checking PostgreSQL setup…
                    </div>
                )}

                {/* No psql found */}
                {embedCheckState?.issue === 'no_psql' && (
                    <div className="p-3 bg-warning/10 border border-warning/20 text-warning text-xs space-y-2 rounded-md">
                        <p><strong>PostgreSQL not found.</strong> Install it to enable code indexing.</p>
                        <ul className="space-y-0.5 text-warning/80">
                            <li><strong>Ubuntu/Debian:</strong> <code className="font-code bg-surface-2 px-1">sudo apt install postgresql postgresql-contrib</code></li>
                            <li><strong>macOS:</strong> <code className="font-code bg-surface-2 px-1">brew install postgresql</code></li>
                            <li><strong>Windows:</strong> Download from <span className="underline">postgresql.org/download/windows</span></li>
                        </ul>
                        <p className="text-warning/60">After installing, also install pgvector: <code className="font-code bg-surface-2 px-1">sudo apt install postgresql-pgvector</code> (Ubuntu) or <code className="font-code bg-surface-2 px-1">brew install pgvector</code> (macOS).</p>
                        <button
                            onClick={runEmbedCheck}
                            className="mt-1 px-3 py-1 text-xs font-bold bg-warning/20 hover:bg-warning/30 text-warning border border-warning/30 transition-colors rounded-md"
                        >
                            Check again
                        </button>
                    </div>
                )}

                {/* Existing URL configured but connection is failing */}
                {embedCheckState?.issue === 'existing_url_broken' && (
                    <div className="p-3 bg-surface-2/60 border border-border-strong text-xs space-y-3 rounded-md">
                        <div className="space-y-1">
                            <p className="text-text font-semibold">Existing connection is failing</p>
                            <p className="text-text-faint font-mono break-all">{embedCheckState.detail}</p>
                            <p className="text-text-faint">Please provide new connection details to reconfigure.</p>
                        </div>
                        <div className="grid grid-cols-2 gap-2">
                            <div className="space-y-1">
                                <Label htmlFor="gen-dbform-host" className="block">Host</Label>
                                <Input id="gen-dbform-host" type="text" value={dbForm.host} onChange={e => setDbForm(f => ({ ...f, host: e.target.value }))} className="font-mono" />
                            </div>
                            <div className="space-y-1">
                                <Label htmlFor="gen-dbform-port" className="block">Port</Label>
                                <Input id="gen-dbform-port" type="text" value={dbForm.port} onChange={e => setDbForm(f => ({ ...f, port: e.target.value }))} className="font-mono" />
                            </div>
                            <div className="space-y-1">
                                <Label htmlFor="gen-dbform-username" className="block">Username</Label>
                                <Input id="gen-dbform-username" type="text" value={dbForm.username} onChange={e => setDbForm(f => ({ ...f, username: e.target.value }))} className="font-mono" autoComplete="off" />
                            </div>
                            <div className="space-y-1">
                                <Label htmlFor="gen-dbform-password" className="block">Password</Label>
                                <Input id="gen-dbform-password" type="password" value={dbForm.password} onChange={e => setDbForm(f => ({ ...f, password: e.target.value }))} className="font-mono" autoComplete="new-password" />
                            </div>
                            <div className="col-span-2 space-y-1">
                                <Label htmlFor="gen-dbform-dbname" className="block">Database Name</Label>
                                <Input id="gen-dbform-dbname" type="text" value={dbForm.dbName} onChange={e => setDbForm(f => ({ ...f, dbName: e.target.value }))} className="font-mono" />
                            </div>
                        </div>
                        {setupError && <p className="text-danger font-mono text-2xs">{setupError}</p>}
                        <div className="flex items-center gap-2 pt-1">
                            <button onClick={handleSetupDb} disabled={setupInProgress} className="flex items-center gap-1.5 px-4 py-2 text-xs font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all disabled:opacity-50 disabled:cursor-not-allowed">
                                {setupInProgress && <Spinner size={12} />}
                                {setupInProgress ? 'Connecting…' : 'Save & Connect'}
                            </button>
                            <button onClick={() => setEmbedCheckState(null)} className="px-3 py-2 text-xs text-text-faint hover:text-text transition-colors">Cancel</button>
                        </div>
                    </div>
                )}

                {/* pgvector missing */}
                {embedCheckState?.issue === 'no_pgvector' && (
                    <div className="p-3 bg-warning/10 border border-warning/20 text-warning text-xs space-y-2 rounded-md">
                        <p><strong>pgvector extension not installed.</strong> PostgreSQL is running but the vector extension is missing.</p>
                        <p>Connect to your database and run:</p>
                        <code className="block bg-surface-2 px-2 py-1.5 font-code text-text">CREATE EXTENSION vector;</code>
                        <p className="text-warning/60">Or install the OS package first: <code className="font-code bg-surface-2 px-1">sudo apt install postgresql-pgvector</code> (Ubuntu) / <code className="font-code bg-surface-2 px-1">brew install pgvector</code> (macOS), then run the SQL above.</p>
                        <button
                            onClick={runEmbedCheck}
                            className="mt-1 px-3 py-1 text-xs font-bold bg-warning/20 hover:bg-warning/30 text-warning border border-warning/30 transition-colors rounded-md"
                        >
                            Check again
                        </button>
                    </div>
                )}

                {/* No DB URL configured — show fresh setup form */}
                {embedCheckState?.issue === 'no_db' && (
                    <div className="p-3 bg-surface-2/60 border border-border-strong text-xs space-y-3 rounded-md">
                        <div className="space-y-1">
                            <p className="text-text font-semibold">Set up a PostgreSQL database for code indexing</p>
                            <p className="text-text-faint">No database configured yet. Enter your PostgreSQL credentials and we'll create the database and enable pgvector.</p>
                        </div>
                        <div className="grid grid-cols-2 gap-2">
                            <div className="space-y-1">
                                <Label htmlFor="gen-dbform-host" className="block">Host</Label>
                                <Input id="gen-dbform-host"
                                    type="text"
                                    value={dbForm.host}
                                    onChange={e => setDbForm(f => ({ ...f, host: e.target.value }))}
                                    className="font-mono"
                                />
                            </div>
                            <div className="space-y-1">
                                <Label htmlFor="gen-dbform-port" className="block">Port</Label>
                                <Input id="gen-dbform-port"
                                    type="text"
                                    value={dbForm.port}
                                    onChange={e => setDbForm(f => ({ ...f, port: e.target.value }))}
                                    className="font-mono"
                                />
                            </div>
                            <div className="space-y-1">
                                <Label htmlFor="gen-dbform-username" className="block">Username</Label>
                                <Input id="gen-dbform-username"
                                    type="text"
                                    value={dbForm.username}
                                    onChange={e => setDbForm(f => ({ ...f, username: e.target.value }))}
                                    className="font-mono"
                                    autoComplete="off"
                                />
                            </div>
                            <div className="space-y-1">
                                <Label htmlFor="gen-dbform-password" className="block">Password</Label>
                                <Input id="gen-dbform-password"
                                    type="password"
                                    value={dbForm.password}
                                    onChange={e => setDbForm(f => ({ ...f, password: e.target.value }))}
                                    className="font-mono"
                                    autoComplete="new-password"
                                />
                            </div>
                            <div className="col-span-2 space-y-1">
                                <Label htmlFor="gen-dbform-dbname" className="block">Database Name</Label>
                                <Input id="gen-dbform-dbname"
                                    type="text"
                                    value={dbForm.dbName}
                                    onChange={e => setDbForm(f => ({ ...f, dbName: e.target.value }))}
                                    className="font-mono"
                                />
                            </div>
                        </div>
                        {setupError && (
                            <p className="text-danger font-mono text-2xs">{setupError}</p>
                        )}
                        <div className="flex items-center gap-2 pt-1">
                            <button
                                onClick={handleSetupDb}
                                disabled={setupInProgress}
                                className="flex items-center gap-1.5 px-4 py-2 text-xs font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                {setupInProgress && <Spinner size={12} />}
                                {setupInProgress ? 'Creating…' : 'Create Database'}
                            </button>
                            <button
                                onClick={() => setEmbedCheckState(null)}
                                className="px-3 py-2 text-xs text-text-faint hover:text-text transition-colors"
                            >
                                Cancel
                            </button>
                        </div>
                    </div>
                )}

                {/* Generic connection error */}
                {embedCheckState?.issue === 'connection_error' && (
                    <div className="p-3 bg-danger/10 border border-danger/20 text-danger text-xs space-y-2 rounded-md">
                        <p><strong>Check failed.</strong> {embedCheckState.detail}</p>
                        <button
                            onClick={runEmbedCheck}
                            className="px-3 py-1 text-xs font-bold bg-danger/20 hover:bg-danger/30 text-danger border border-danger/30 transition-colors rounded-md"
                        >
                            Try again
                        </button>
                    </div>
                )}
            </div>

            {/* Bash Command Directories */}
            <div className="space-y-4">
                <Label size="sm" className="block">Allowed Directories</Label>
                <p className="text-xs text-text-faint">
                    Directories the bash tool and filesystem MCP server can access.
                    Linked repos and vault are always included automatically.
                </p>
                {bashAllowedDirs.length > 0 && (
                    <div className="space-y-1">
                        {bashAllowedDirs.map((dir, i) => (
                            <div key={i} className="flex items-center justify-between bg-surface border border-border px-3 py-2 rounded-md">
                                <span className="text-xs font-mono text-text truncate">{dir}</span>
                                <button
                                    onClick={() => setBashAllowedDirs(bashAllowedDirs.filter((_, j) => j !== i))}
                                    className="text-text-faint hover:text-danger transition-colors text-xs ml-2 flex-shrink-0"
                                >
                                    Remove
                                </button>
                            </div>
                        ))}
                    </div>
                )}
                <div className="flex gap-2">
                    <Input
                        type="text"
                        value={newDir}
                        onChange={e => setNewDir(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter' && newDir.trim()) {
                                setBashAllowedDirs([...bashAllowedDirs, newDir.trim()]);
                                setNewDir('');
                            }
                        }}
                        placeholder="/path/to/directory"
                        aria-label="Directory to allow"
                        className="flex-1 font-mono"
                    />
                    <button
                        onClick={() => {
                            if (newDir.trim()) {
                                setBashAllowedDirs([...bashAllowedDirs, newDir.trim()]);
                                setNewDir('');
                            }
                        }}
                        disabled={!newDir.trim()}
                        className="px-4 py-2.5 text-xs font-bold bg-surface hover:bg-surface-2 text-text transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                        Add
                    </button>
                </div>
            </div>

            {/* Transform Step Python Runtime */}
            <div className="space-y-4">
                <Label size="sm" className="block">Transform Step Python Runtime</Label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-text-faint mt-0.5">
                            Where Transform-step Python code runs. <strong className="text-text-muted">Docker</strong> (default) sandboxes execution with 512 MB / 1 CPU / 60s caps and no GPU access — safe but limited.
                            <strong className="text-text-muted"> Host</strong> runs Python directly on the host with full RAM, GPU, filesystem, and network.
                        </p>
                    </div>
                    <button
                        role="switch"
                        aria-checked={transformRuntime === 'host'}
                        aria-label="Run Transform steps on the host"
                        onClick={() => {
                            if (transformRuntime === 'docker') {
                                setShowHostRuntimeModal(true);
                            } else {
                                setTransformRuntime('docker');
                            }
                        }}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 ${transformRuntime === 'host' ? 'bg-warning' : 'bg-surface-2'}`}
                    >
                        <span className={`inline-block h-4 w-4 transform rounded-full transition-transform ${transformRuntime === 'host' ? 'translate-x-6 bg-accent-fg' : 'translate-x-1 bg-text-muted'}`} />
                    </button>
                </div>
                {transformRuntime === 'host' && (
                    <div className="p-3 bg-warning/10 border border-warning/20 text-warning text-xs flex items-start gap-2 rounded-md">
                        <AlertTriangle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                        <div>
                            <strong>Host mode active.</strong> Any Python in any Transform step now runs unsandboxed with full backend permissions. Only use on self-hosted instances you control.
                        </div>
                    </div>
                )}
            </div>

            {/* Host-mode confirmation modal */}
            {showHostRuntimeModal && (
                <Modal
                    open
                    onClose={() => setShowHostRuntimeModal(false)}
                    title="Enable host-mode Transform execution?"
                    description="This removes the Docker sandbox from Transform steps."
                    footer={
                        <>
                            <Button variant="secondary" onClick={() => setShowHostRuntimeModal(false)}>
                                Cancel
                            </Button>
                            <Button
                                variant="danger"
                                onClick={() => {
                                    setTransformRuntime('host');
                                    setShowHostRuntimeModal(false);
                                }}
                            >
                                I understand — enable host mode
                            </Button>
                        </>
                    }
                >
                    <div className="space-y-2 text-sm leading-relaxed text-text-muted">
                        <p>With host mode on, Transform-step Python code runs as a subprocess on the host machine with:</p>
                        <ul className="list-inside list-disc space-y-1 pl-1 text-text-faint">
                            <li>Full backend filesystem access (including <code className="font-code text-text">backend/data/</code>)</li>
                            <li>Full network access</li>
                            <li>Access to any GPU and all host RAM</li>
                            <li>The same permissions as the Synapse backend process</li>
                        </ul>
                        <p className="pt-1 text-warning">
                            Only enable this on a self-hosted instance you control. It is the wrong
                            choice for any deployment where untrusted code or untrusted users can
                            reach the Transform step.
                        </p>
                    </div>
                </Modal>
            )}

            {/* Login & Security */}
            <div className="space-y-4">
                <Label size="sm" className="block">Login &amp; Security</Label>

                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-sm text-text font-medium">Require Login</p>
                        <p className="text-xs text-text-faint mt-0.5">Protect Synapse with a username and password.</p>
                    </div>
                    <button
                        role="switch"
                        aria-checked={loginEnabled || enabling}
                        aria-label="Require login"
                        disabled={isLoginSaving}
                        onClick={() => {
                            setLoginFormError('');
                            if (loginEnabled) {
                                // Off is saved at once, after a confirm; the
                                // switch moves when the save succeeds.
                                setConfirmLoginOff(true);
                            } else if (enabling) {
                                closeLoginForm();
                            } else {
                                // On needs credentials first: the form saves it.
                                setEnabling(true);
                                setShowLoginForm(true);
                            }
                        }}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 disabled:opacity-60 ${loginEnabled || enabling ? 'bg-accent' : 'bg-surface-2'}`}
                    >
                        <span className={`inline-block h-4 w-4 transform rounded-full transition-transform ${loginEnabled || enabling ? 'translate-x-6 bg-accent-fg' : 'translate-x-1 bg-text-muted'}`} />
                    </button>
                </div>

                <ConfirmDialog
                    open={confirmLoginOff}
                    onClose={() => setConfirmLoginOff(false)}
                    onConfirm={async () => {
                        await onSaveLogin(false, '', '');
                        setConfirmLoginOff(false);
                    }}
                    title="Turn off login?"
                    description="Anyone who can open this address will be able to use Synapse, with no password. You can turn it back on at any time."
                    confirmLabel="Turn off login"
                    busy={isLoginSaving}
                />

                {/* Configured state: show summary + actions. Turning login off
                    is the switch's job, so it has no second control here. */}
                {loginEnabled && !showLoginForm && loginUsername && (
                    <div className="p-3 bg-surface border border-border space-y-2 rounded-md">
                        <p className="text-xs text-text-muted">
                            Login enabled for user: <span className="text-text font-mono">{loginUsername}</span>
                        </p>
                        <div className="flex gap-4">
                            <button
                                onClick={() => { setShowLoginForm(true); setLoginFormError(''); }}
                                className="text-xs text-text-muted hover:text-text transition-colors"
                            >
                                Change Password
                            </button>
                        </div>
                    </div>
                )}

                {/* Credentials form */}
                {(loginEnabled || enabling) && (showLoginForm || !loginUsername) && (
                    <div className="p-4 bg-surface border border-border space-y-3 rounded-md">
                        <p className="text-xs font-bold text-text">
                            {loginUsername ? 'Update Credentials' : 'Set Login Credentials'}
                        </p>

                        <div className="space-y-1">
                            <Label htmlFor="gen-loginusername" className="block">Username</Label>
                            <Input id="gen-loginusername"
                                type="text"
                                value={loginUsername}
                                onChange={e => setLoginUsername(e.target.value)}
                                autoComplete="off"
                                
                                placeholder="admin"
                            />
                        </div>

                        <div className="space-y-1">
                            <Label htmlFor="gen-login-password" className="block">
                                {loginUsername ? 'New Password' : 'Password'}
                            </Label>
                            <Input
                                id="gen-login-password"
                                type="password"
                                value={loginPassword}
                                onChange={e => setLoginPassword(e.target.value)}
                                autoComplete="new-password"
                                
                                placeholder={loginUsername ? 'Leave blank to keep current' : 'Min. 8 characters'}
                            />
                        </div>

                        <div className="space-y-1">
                            <Label htmlFor="gen-loginconfirmpassword" className="block">Confirm Password</Label>
                            <Input id="gen-loginconfirmpassword"
                                type="password"
                                value={loginConfirmPassword}
                                onChange={e => setLoginConfirmPassword(e.target.value)}
                                autoComplete="new-password"
                                
                            />
                        </div>

                        {loginFormError && (
                            <p className="text-danger text-xs">{loginFormError}</p>
                        )}

                        <div className="flex gap-2 pt-1">
                            <button
                                onClick={async () => {
                                    setLoginFormError('');
                                    if (!loginUsername.trim()) {
                                        setLoginFormError('Username is required.');
                                        return;
                                    }
                                    if (loginPassword && loginPassword !== loginConfirmPassword) {
                                        setLoginFormError('Passwords do not match.');
                                        return;
                                    }
                                    if (!loginPassword && !loginUsername) {
                                        setLoginFormError('Password is required for first-time setup.');
                                        return;
                                    }
                                    if (loginPassword && loginPassword.length < 8) {
                                        setLoginFormError('Password must be at least 8 characters.');
                                        return;
                                    }
                                    // A failed save keeps the form, and what was typed.
                                    if (await onSaveLogin(true, loginUsername, loginPassword)) {
                                        closeLoginForm();
                                    }
                                }}
                                disabled={isLoginSaving}
                                className="flex items-center gap-1.5 px-4 py-2 text-xs font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all disabled:opacity-50"
                            >
                                {isLoginSaving && <Spinner size={12} />}
                                {isLoginSaving ? 'Saving…' : 'Save Login Settings'}
                            </button>
                            <button
                                onClick={closeLoginForm}
                                className="px-3 py-2 text-xs text-text-faint hover:text-text transition-colors"
                            >
                                Cancel
                            </button>
                        </div>

                        <p className="text-xs text-text-faint pt-1">
                            Forgot your password? Run{' '}
                            <code className="text-text-muted bg-bg px-1 font-code text-2xs">synapse reset-password</code>
                            {' '}in your terminal.
                        </p>
                    </div>
                )}
            </div>

            <div className="pt-4 flex justify-end">
                <button
                    onClick={onSave}
                    disabled={isSaving}
                    className="flex items-center gap-2 px-6 py-2.5 text-sm font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all shadow-lg disabled:opacity-50 disabled:cursor-not-allowed"
                >
                    {isSaving && <Spinner size={16} />}
                    {isSaving ? 'Saving…' : 'Save Changes'}
                </button>
            </div>
        </div>
    );
}
