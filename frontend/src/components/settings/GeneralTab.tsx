import { useState, useEffect } from 'react';
import { Loader2 } from 'lucide-react';

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
    embedCode: boolean;
    setEmbedCode: (v: boolean) => void;
    bashAllowedDirs: string[];
    setBashAllowedDirs: (v: string[]) => void;
    loginEnabled: boolean;
    setLoginEnabled: (v: boolean) => void;
    loginUsername: string;
    setLoginUsername: (v: string) => void;
    onSaveLogin: (enabled: boolean, username: string, password: string) => Promise<void>;
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
    embedCode, setEmbedCode,
    bashAllowedDirs, setBashAllowedDirs,
    loginEnabled, setLoginEnabled,
    loginUsername, setLoginUsername,
    onSaveLogin, isLoginSaving,
    onSave, isSaving,
}: GeneralTabProps) {
    const [embedChecking, setEmbedChecking] = useState(false);
    const [newDir, setNewDir] = useState('');
    const [vaultDraft, setVaultDraft] = useState(String(vaultThreshold));
    const [compactDraft, setCompactDraft] = useState(String(autoCompactThreshold));

    // Login form local state
    const [showLoginForm, setShowLoginForm] = useState(false);
    const [loginPassword, setLoginPassword] = useState('');
    const [loginConfirmPassword, setLoginConfirmPassword] = useState('');
    const [loginFormError, setLoginFormError] = useState('');

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
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Global Agent Name</label>
                <input
                    type="text"
                    value={agentName}
                    onChange={(e) => setAgentName(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-sm focus:border-white focus:outline-none transition-colors text-white placeholder:text-zinc-700 font-medium"
                    placeholder="Enter Agent Name"
                />
                <p className="text-xs text-zinc-600">This name identifies your agent across the system.</p>
            </div>

            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Large Response Handling</label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-zinc-600 mt-0.5">When enabled, tool outputs exceeding the threshold are saved to a vault file instead of flooding the context.</p>
                    </div>
                    <button
                        onClick={() => setVaultEnabled(!vaultEnabled)}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none ${vaultEnabled ? 'bg-white' : 'bg-zinc-700'}`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full transition-transform ${vaultEnabled ? 'translate-x-6 bg-black' : 'translate-x-1 bg-zinc-400'}`}
                        />
                    </button>
                </div>
                {vaultEnabled && (
                    <div className="space-y-2">
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Character Threshold</label>
                        <p className="text-xs text-zinc-500">
                            ≈ <span className="text-zinc-300 font-semibold">{Math.round(vaultThreshold / 4).toLocaleString()}</span> tokens
                            <span className="text-zinc-600 ml-1">(at ~4 chars / token)</span>
                        </p>
                        <input
                            type="number"
                            value={vaultDraft}
                            onChange={(e) => setVaultDraft(e.target.value)}
                            onBlur={() => {
                                const v = Math.max(1, parseInt(vaultDraft) || 1);
                                setVaultThreshold(v);
                                setVaultDraft(String(v));
                            }}
                            className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-sm focus:border-white focus:outline-none transition-colors text-white placeholder:text-zinc-700 font-medium"
                            min={1}
                        />
                        <p className="text-xs text-zinc-600">Responses longer than this many characters will be saved to a file.</p>
                    </div>
                )}
            </div>

            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Auto Context Compaction</label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-zinc-600 mt-0.5">
                            When the accumulated context exceeds the threshold, the agent summarises everything so far to ~30% of its size and archives the original to the vault so nothing is lost.
                        </p>
                    </div>
                    <button
                        onClick={() => setAutoCompactEnabled(!autoCompactEnabled)}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 ${autoCompactEnabled ? 'bg-white' : 'bg-zinc-700'}`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full transition-transform ${autoCompactEnabled ? 'translate-x-6 bg-black' : 'translate-x-1 bg-zinc-400'}`}
                        />
                    </button>
                </div>
                {autoCompactEnabled && (
                    <div className="space-y-2">
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Compaction Threshold (characters)</label>
                        <p className="text-xs text-zinc-500">
                            ≈ <span className="text-zinc-300 font-semibold">{Math.round(autoCompactThreshold / 4).toLocaleString()}</span> tokens
                            <span className="text-zinc-600 ml-1">(at ~4 chars / token)</span>
                        </p>
                        <input
                            type="number"
                            value={compactDraft}
                            onChange={(e) => setCompactDraft(e.target.value)}
                            onBlur={() => {
                                const v = Math.max(10000, parseInt(compactDraft) || 10000);
                                setAutoCompactThreshold(v);
                                setCompactDraft(String(v));
                            }}
                            className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-sm focus:border-white focus:outline-none transition-colors text-white placeholder:text-zinc-700 font-medium"
                            min={10000}
                        />
                        <p className="text-xs text-zinc-600">
                            When context exceeds this, it is compacted using the current model. The full original is archived to the vault.
                        </p>
                    </div>
                )}
            </div>

            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Database Write Access</label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-zinc-600 mt-0.5">
                            When disabled (default), agents are strictly limited to SELECT/SHOW/DESCRIBE queries.
                            When enabled, INSERT/UPDATE/DELETE and other write queries are allowed — but agents must always ask for confirmation before executing them.
                        </p>
                    </div>
                    <button
                        onClick={() => setAllowDbWrite(!allowDbWrite)}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 ${allowDbWrite ? 'bg-amber-500' : 'bg-zinc-700'}`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full transition-transform ${allowDbWrite ? 'translate-x-6 bg-black' : 'translate-x-1 bg-zinc-400'}`}
                        />
                    </button>
                </div>
                {allowDbWrite && (
                    <div className="p-3 bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
                        <strong>Write mode active.</strong> Agents MUST ask for explicit user confirmation before running any INSERT, UPDATE, DELETE, DROP, or CREATE queries. This is enforced in the system prompt.
                    </div>
                )}
            </div>

            {/* Code Repository Indexing */}
            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Code Repository Indexing</label>
                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-xs text-zinc-600 mt-0.5">
                            When enabled, agents can semantically search your indexed code repositories using vector embeddings.
                            Requires PostgreSQL with the pgvector extension.
                        </p>
                    </div>
                    <button
                        onClick={handleEmbedToggle}
                        disabled={embedChecking}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 ${embedCode ? 'bg-white' : 'bg-zinc-700'} ${embedChecking ? 'opacity-50 cursor-not-allowed' : ''}`}
                    >
                        <span
                            className={`inline-block h-4 w-4 transform rounded-full transition-transform ${embedCode ? 'translate-x-6 bg-black' : 'translate-x-1 bg-zinc-400'}`}
                        />
                    </button>
                </div>

                {/* Checking state */}
                {embedChecking && (
                    <div className="flex items-center gap-2 text-xs text-zinc-400">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        Checking PostgreSQL setup…
                    </div>
                )}

                {/* No psql found */}
                {embedCheckState?.issue === 'no_psql' && (
                    <div className="p-3 bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs space-y-2">
                        <p><strong>PostgreSQL not found.</strong> Install it to enable code indexing.</p>
                        <ul className="space-y-0.5 text-amber-300/80">
                            <li><strong>Ubuntu/Debian:</strong> <code className="bg-black/30 px-1">sudo apt install postgresql postgresql-contrib</code></li>
                            <li><strong>macOS:</strong> <code className="bg-black/30 px-1">brew install postgresql</code></li>
                            <li><strong>Windows:</strong> Download from <span className="underline">postgresql.org/download/windows</span></li>
                        </ul>
                        <p className="text-amber-300/60">After installing, also install pgvector: <code className="bg-black/30 px-1">sudo apt install postgresql-pgvector</code> (Ubuntu) or <code className="bg-black/30 px-1">brew install pgvector</code> (macOS).</p>
                        <button
                            onClick={runEmbedCheck}
                            className="mt-1 px-3 py-1 text-xs font-bold bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/30 transition-colors"
                        >
                            Check again
                        </button>
                    </div>
                )}

                {/* Existing URL configured but connection is failing */}
                {embedCheckState?.issue === 'existing_url_broken' && (
                    <div className="p-3 bg-zinc-800/60 border border-zinc-700 text-xs space-y-3">
                        <div className="space-y-1">
                            <p className="text-zinc-300 font-semibold">Existing connection is failing</p>
                            <p className="text-zinc-500 font-mono break-all">{embedCheckState.detail}</p>
                            <p className="text-zinc-500">Please provide new connection details to reconfigure.</p>
                        </div>
                        <div className="grid grid-cols-2 gap-2">
                            <div className="space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Host</label>
                                <input type="text" value={dbForm.host} onChange={e => setDbForm(f => ({ ...f, host: e.target.value }))} className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs" />
                            </div>
                            <div className="space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Port</label>
                                <input type="text" value={dbForm.port} onChange={e => setDbForm(f => ({ ...f, port: e.target.value }))} className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs" />
                            </div>
                            <div className="space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Username</label>
                                <input type="text" value={dbForm.username} onChange={e => setDbForm(f => ({ ...f, username: e.target.value }))} className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs" autoComplete="off" />
                            </div>
                            <div className="space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Password</label>
                                <input type="password" value={dbForm.password} onChange={e => setDbForm(f => ({ ...f, password: e.target.value }))} className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs" autoComplete="new-password" />
                            </div>
                            <div className="col-span-2 space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Database Name</label>
                                <input type="text" value={dbForm.dbName} onChange={e => setDbForm(f => ({ ...f, dbName: e.target.value }))} className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs" />
                            </div>
                        </div>
                        {setupError && <p className="text-red-400 font-mono text-[11px]">{setupError}</p>}
                        <div className="flex items-center gap-2 pt-1">
                            <button onClick={handleSetupDb} disabled={setupInProgress} className="flex items-center gap-1.5 px-4 py-2 text-xs font-bold bg-white text-black hover:bg-zinc-200 transition-all disabled:opacity-50 disabled:cursor-not-allowed">
                                {setupInProgress && <Loader2 className="w-3 h-3 animate-spin" />}
                                {setupInProgress ? 'Connecting…' : 'Save & Connect'}
                            </button>
                            <button onClick={() => setEmbedCheckState(null)} className="px-3 py-2 text-xs text-zinc-500 hover:text-white transition-colors">Cancel</button>
                        </div>
                    </div>
                )}

                {/* pgvector missing */}
                {embedCheckState?.issue === 'no_pgvector' && (
                    <div className="p-3 bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs space-y-2">
                        <p><strong>pgvector extension not installed.</strong> PostgreSQL is running but the vector extension is missing.</p>
                        <p>Connect to your database and run:</p>
                        <code className="block bg-black/40 px-2 py-1.5 font-mono text-white/80">CREATE EXTENSION vector;</code>
                        <p className="text-amber-300/60">Or install the OS package first: <code className="bg-black/30 px-1">sudo apt install postgresql-pgvector</code> (Ubuntu) / <code className="bg-black/30 px-1">brew install pgvector</code> (macOS), then run the SQL above.</p>
                        <button
                            onClick={runEmbedCheck}
                            className="mt-1 px-3 py-1 text-xs font-bold bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/30 transition-colors"
                        >
                            Check again
                        </button>
                    </div>
                )}

                {/* No DB URL configured — show fresh setup form */}
                {embedCheckState?.issue === 'no_db' && (
                    <div className="p-3 bg-zinc-800/60 border border-zinc-700 text-xs space-y-3">
                        <div className="space-y-1">
                            <p className="text-zinc-300 font-semibold">Set up a PostgreSQL database for code indexing</p>
                            <p className="text-zinc-500">No database configured yet. Enter your PostgreSQL credentials and we'll create the database and enable pgvector.</p>
                        </div>
                        <div className="grid grid-cols-2 gap-2">
                            <div className="space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Host</label>
                                <input
                                    type="text"
                                    value={dbForm.host}
                                    onChange={e => setDbForm(f => ({ ...f, host: e.target.value }))}
                                    className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs"
                                />
                            </div>
                            <div className="space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Port</label>
                                <input
                                    type="text"
                                    value={dbForm.port}
                                    onChange={e => setDbForm(f => ({ ...f, port: e.target.value }))}
                                    className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs"
                                />
                            </div>
                            <div className="space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Username</label>
                                <input
                                    type="text"
                                    value={dbForm.username}
                                    onChange={e => setDbForm(f => ({ ...f, username: e.target.value }))}
                                    className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs"
                                    autoComplete="off"
                                />
                            </div>
                            <div className="space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Password</label>
                                <input
                                    type="password"
                                    value={dbForm.password}
                                    onChange={e => setDbForm(f => ({ ...f, password: e.target.value }))}
                                    className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs"
                                    autoComplete="new-password"
                                />
                            </div>
                            <div className="col-span-2 space-y-1">
                                <label className="text-zinc-500 uppercase tracking-wider text-[10px] font-bold">Database Name</label>
                                <input
                                    type="text"
                                    value={dbForm.dbName}
                                    onChange={e => setDbForm(f => ({ ...f, dbName: e.target.value }))}
                                    className="w-full bg-zinc-900 border border-zinc-700 p-2 text-white focus:border-white focus:outline-none font-mono text-xs"
                                />
                            </div>
                        </div>
                        {setupError && (
                            <p className="text-red-400 font-mono text-[11px]">{setupError}</p>
                        )}
                        <div className="flex items-center gap-2 pt-1">
                            <button
                                onClick={handleSetupDb}
                                disabled={setupInProgress}
                                className="flex items-center gap-1.5 px-4 py-2 text-xs font-bold bg-white text-black hover:bg-zinc-200 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                {setupInProgress && <Loader2 className="w-3 h-3 animate-spin" />}
                                {setupInProgress ? 'Creating…' : 'Create Database'}
                            </button>
                            <button
                                onClick={() => setEmbedCheckState(null)}
                                className="px-3 py-2 text-xs text-zinc-500 hover:text-white transition-colors"
                            >
                                Cancel
                            </button>
                        </div>
                    </div>
                )}

                {/* Generic connection error */}
                {embedCheckState?.issue === 'connection_error' && (
                    <div className="p-3 bg-red-500/10 border border-red-500/20 text-red-400 text-xs space-y-2">
                        <p><strong>Check failed.</strong> {embedCheckState.detail}</p>
                        <button
                            onClick={runEmbedCheck}
                            className="px-3 py-1 text-xs font-bold bg-red-500/20 hover:bg-red-500/30 text-red-300 border border-red-500/30 transition-colors"
                        >
                            Try again
                        </button>
                    </div>
                )}
            </div>

            {/* Bash Command Directories */}
            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Bash Command Directories</label>
                <p className="text-xs text-zinc-600">
                    Extra directories the bash tool can execute commands in.
                    Linked repos and vault are always included automatically.
                </p>
                {bashAllowedDirs.length > 0 && (
                    <div className="space-y-1">
                        {bashAllowedDirs.map((dir, i) => (
                            <div key={i} className="flex items-center justify-between bg-zinc-900 border border-zinc-800 px-3 py-2">
                                <span className="text-xs font-mono text-zinc-300 truncate">{dir}</span>
                                <button
                                    onClick={() => setBashAllowedDirs(bashAllowedDirs.filter((_, j) => j !== i))}
                                    className="text-zinc-600 hover:text-red-400 transition-colors text-xs ml-2 flex-shrink-0"
                                >
                                    Remove
                                </button>
                            </div>
                        ))}
                    </div>
                )}
                <div className="flex gap-2">
                    <input
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
                        className="flex-1 bg-zinc-900 border border-zinc-800 p-2.5 text-sm focus:border-white focus:outline-none transition-colors text-white placeholder:text-zinc-700 font-mono"
                    />
                    <button
                        onClick={() => {
                            if (newDir.trim()) {
                                setBashAllowedDirs([...bashAllowedDirs, newDir.trim()]);
                                setNewDir('');
                            }
                        }}
                        disabled={!newDir.trim()}
                        className="px-4 py-2.5 text-xs font-bold bg-zinc-800 hover:bg-zinc-700 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                        Add
                    </button>
                </div>
            </div>

            {/* Login & Security */}
            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Login &amp; Security</label>

                <div className="flex items-center justify-between">
                    <div>
                        <p className="text-sm text-zinc-300 font-medium">Require Login</p>
                        <p className="text-xs text-zinc-600 mt-0.5">Protect Synapse with a username and password.</p>
                    </div>
                    <button
                        onClick={() => {
                            if (!loginEnabled) {
                                setShowLoginForm(true);
                            }
                            setLoginEnabled(!loginEnabled);
                            setLoginFormError('');
                        }}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none flex-shrink-0 ml-4 ${loginEnabled ? 'bg-white' : 'bg-zinc-700'}`}
                    >
                        <span className={`inline-block h-4 w-4 transform rounded-full transition-transform ${loginEnabled ? 'translate-x-6 bg-black' : 'translate-x-1 bg-zinc-400'}`} />
                    </button>
                </div>

                {/* Configured state: show summary + actions */}
                {loginEnabled && !showLoginForm && loginUsername && (
                    <div className="p-3 bg-zinc-900 border border-zinc-800 space-y-2">
                        <p className="text-xs text-zinc-400">
                            Login enabled for user: <span className="text-white font-mono">{loginUsername}</span>
                        </p>
                        <div className="flex gap-4">
                            <button
                                onClick={() => { setShowLoginForm(true); setLoginFormError(''); }}
                                className="text-xs text-zinc-400 hover:text-white transition-colors"
                            >
                                Change Password
                            </button>
                            <button
                                onClick={() => onSaveLogin(false, '', '')}
                                className="text-xs text-red-500 hover:text-red-400 transition-colors"
                            >
                                Disable Login
                            </button>
                        </div>
                    </div>
                )}

                {/* Credentials form */}
                {loginEnabled && (showLoginForm || !loginUsername) && (
                    <div className="p-4 bg-zinc-900 border border-zinc-800 space-y-3">
                        <p className="text-xs font-bold text-zinc-300">
                            {loginUsername ? 'Update Credentials' : 'Set Login Credentials'}
                        </p>

                        <div className="space-y-1">
                            <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">Username</label>
                            <input
                                type="text"
                                value={loginUsername}
                                onChange={e => setLoginUsername(e.target.value)}
                                autoComplete="off"
                                className="w-full bg-zinc-950 border border-zinc-700 p-2 text-sm focus:border-white focus:outline-none text-white"
                                placeholder="admin"
                            />
                        </div>

                        <div className="space-y-1">
                            <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">
                                {loginUsername ? 'New Password' : 'Password'}
                            </label>
                            <input
                                type="password"
                                value={loginPassword}
                                onChange={e => setLoginPassword(e.target.value)}
                                autoComplete="new-password"
                                className="w-full bg-zinc-950 border border-zinc-700 p-2 text-sm focus:border-white focus:outline-none text-white"
                                placeholder={loginUsername ? 'Leave blank to keep current' : 'Min. 8 characters'}
                            />
                        </div>

                        <div className="space-y-1">
                            <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">Confirm Password</label>
                            <input
                                type="password"
                                value={loginConfirmPassword}
                                onChange={e => setLoginConfirmPassword(e.target.value)}
                                autoComplete="new-password"
                                className="w-full bg-zinc-950 border border-zinc-700 p-2 text-sm focus:border-white focus:outline-none text-white"
                            />
                        </div>

                        {loginFormError && (
                            <p className="text-red-400 text-xs">{loginFormError}</p>
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
                                    await onSaveLogin(true, loginUsername, loginPassword);
                                    setShowLoginForm(false);
                                    setLoginPassword('');
                                    setLoginConfirmPassword('');
                                }}
                                disabled={isLoginSaving}
                                className="flex items-center gap-1.5 px-4 py-2 text-xs font-bold bg-white text-black hover:bg-zinc-200 transition-all disabled:opacity-50"
                            >
                                {isLoginSaving && <Loader2 className="w-3 h-3 animate-spin" />}
                                {isLoginSaving ? 'Saving…' : 'Save Login Settings'}
                            </button>
                            <button
                                onClick={() => {
                                    setShowLoginForm(false);
                                    setLoginFormError('');
                                    setLoginPassword('');
                                    setLoginConfirmPassword('');
                                    if (!loginUsername) setLoginEnabled(false);
                                }}
                                className="px-3 py-2 text-xs text-zinc-500 hover:text-white transition-colors"
                            >
                                Cancel
                            </button>
                        </div>

                        <p className="text-xs text-zinc-600 pt-1">
                            Forgot your password? Run{' '}
                            <code className="text-zinc-400 bg-zinc-950 px-1 font-mono text-[11px]">synapse reset-password</code>
                            {' '}in your terminal.
                        </p>
                    </div>
                )}
            </div>

            <div className="pt-4 flex justify-end">
                <button
                    onClick={onSave}
                    disabled={isSaving}
                    className="flex items-center gap-2 px-6 py-2.5 text-sm font-bold bg-white text-black hover:bg-zinc-200 transition-all shadow-lg disabled:opacity-50 disabled:cursor-not-allowed"
                >
                    {isSaving && <Loader2 className="w-4 h-4 animate-spin" />}
                    {isSaving ? 'Saving…' : 'Save Changes'}
                </button>
            </div>
        </div>
    );
}
