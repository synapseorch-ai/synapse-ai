import { useState, useEffect } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Button, ConfirmDialog, Input, Label, Section, Select, Spinner, Switch } from '@/components/ui';
import { FIELD_LABEL, FORM } from './form';
import { SaveBar } from './SaveBar';
import { ToolPolicyFields, type ToolPolicy } from './ToolPolicy';
import { WORKSPACE_TURN_LIMIT_CHOICES } from './turnLimit';


/** A setting that is on or off: what it does, and its switch at the end of the line. */
function SwitchRow({ label, children, checked, onChange, disabled }: {
    label: string; children: React.ReactNode; checked: boolean; onChange: () => void; disabled?: boolean;
}) {
    return (
        <div className={`flex items-start justify-between gap-6 ${FORM}`}>
            <p className="text-sm leading-relaxed text-text-muted">{children}</p>
            <Switch checked={checked} onChange={onChange} label={label} disabled={disabled} className="mt-0.5" />
        </div>
    );
}

/** A note under a setting: a caveat (neutral-strong, the `warning` alias) or a failure. */
function Note({ tone = 'warning', children }: { tone?: 'warning' | 'danger'; children: React.ReactNode }) {
    return (
        <div className={`${FORM} space-y-2 rounded-md border px-3 py-2.5 text-xs leading-relaxed ${tone === 'danger'
            ? 'border-danger/25 bg-danger-subtle text-danger'
            : 'border-warning/25 bg-warning-subtle text-text'}`}>
            {children}
        </div>
    );
}

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

export type BoardsSettings = { enabled: boolean; maxOpenCards: number; maxShared: number };

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
    /** Knowledge's switch (`knowledge_enabled`, F367); null until the settings are read. */
    knowledgeEnabled: boolean | null;
    setKnowledgeEnabled: (v: boolean) => void;
    /** Boards' switch and caps (`boards_enabled`, `boards_max_open_cards`, `boards_max_shared`; BOARD1b); null until read. */
    boards: BoardsSettings | null;
    setBoards: (v: BoardsSettings) => void;
    /** The most members a Team agent may run at once on this install (`team_at_once_ceiling`; TEAM1b); null until read. */
    teamCeiling: number | null;
    setTeamCeiling: (v: number) => void;
    /** What an agent does at its turn limit (`turn_limit_mode`, F1219): '' is the default; null until read. */
    turnLimitMode: string | null;
    setTurnLimitMode: (v: string) => void;
    toolPolicyDefaults: ToolPolicy;
    setToolPolicyDefaults: (v: ToolPolicy) => void;
    embedCode: boolean;
    setEmbedCode: (v: boolean) => void;
    bashAllowedDirs: string[];
    setBashAllowedDirs: (v: string[]) => void;
    /** Where this server runs Transform steps: its operator's choice, shown, never set here (F968). */
    transformRuntime: 'docker' | 'host';
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
    knowledgeEnabled, setKnowledgeEnabled,
    boards, setBoards,
    teamCeiling, setTeamCeiling,
    turnLimitMode, setTurnLimitMode,
    toolPolicyDefaults, setToolPolicyDefaults,
    embedCode, setEmbedCode,
    bashAllowedDirs, setBashAllowedDirs,
    transformRuntime,
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

    const S = { variant: 'plain' as const, collapsible: false };
    const addDir = () => {
        if (newDir.trim()) {
            setBashAllowedDirs([...bashAllowedDirs, newDir.trim()]);
            setNewDir('');
        }
    };
    const dbFields = (
        <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
                <Label htmlFor="gen-dbform-host" className={FIELD_LABEL}>Host</Label>
                <Input id="gen-dbform-host" type="text" value={dbForm.host} onChange={e => setDbForm(f => ({ ...f, host: e.target.value }))} className="font-code" />
            </div>
            <div className="space-y-1">
                <Label htmlFor="gen-dbform-port" className={FIELD_LABEL}>Port</Label>
                <Input id="gen-dbform-port" type="text" value={dbForm.port} onChange={e => setDbForm(f => ({ ...f, port: e.target.value }))} className="font-code" />
            </div>
            <div className="space-y-1">
                <Label htmlFor="gen-dbform-username" className={FIELD_LABEL}>Username</Label>
                <Input id="gen-dbform-username" type="text" value={dbForm.username} onChange={e => setDbForm(f => ({ ...f, username: e.target.value }))} className="font-code" autoComplete="off" />
            </div>
            <div className="space-y-1">
                <Label htmlFor="gen-dbform-password" className={FIELD_LABEL}>Password</Label>
                <Input id="gen-dbform-password" type="password" value={dbForm.password} onChange={e => setDbForm(f => ({ ...f, password: e.target.value }))} className="font-code" autoComplete="new-password" />
            </div>
            <div className="col-span-2 space-y-1">
                <Label htmlFor="gen-dbform-dbname" className={FIELD_LABEL}>Database name</Label>
                <Input id="gen-dbform-dbname" type="text" value={dbForm.dbName} onChange={e => setDbForm(f => ({ ...f, dbName: e.target.value }))} className="font-code" />
            </div>
        </div>
    );
    const code = 'rounded bg-surface-2 px-1 font-code text-text';

    return (
        <div>
            <Section {...S} title="Global agent name">
                <div className={`space-y-1.5 ${FORM}`}>
                    <Input id="gen-agentname"
                        type="text"
                        aria-label="Global agent name"
                        value={agentName}
                        onChange={(e) => setAgentName(e.target.value)}
                        className="font-medium"
                        placeholder="Enter agent name"
                    />
                    <p className="text-xs text-text-faint">This name identifies your agent across the system.</p>
                </div>
            </Section>

            <Section {...S} title="Knowledge">
                <SwitchRow label="Knowledge" checked={!!knowledgeEnabled} disabled={knowledgeEnabled === null}
                    onChange={() => setKnowledgeEnabled(!knowledgeEnabled)}>
                    Makes Vault folders searchable, so agents and workflows answer from your documents and cite them. Once it is saved, choose an embedding model in Settings → Models.
                </SwitchRow>
            </Section>

            <Section {...S} title="Boards">
                <div className="space-y-4">
                    <SwitchRow label="Boards" checked={!!boards?.enabled} disabled={boards === null}
                        onChange={() => boards && setBoards({ ...boards, enabled: !boards.enabled })}>
                        Cards of shared work that agents read, claim and close. Each workflow and each scheduled agent gets its own board, kept across runs, and shared boards are used by what their lists name. An agent&apos;s Boards section turns its own board off.
                    </SwitchRow>
                    {boards?.enabled && (
                        <div className={`grid gap-4 sm:grid-cols-2 ${FORM}`}>
                            <div className="space-y-1.5">
                                <Label htmlFor="gen-boards-open" className={FIELD_LABEL}>Open cards per board</Label>
                                <Input id="gen-boards-open" type="number" min={1} className="w-40 font-medium tabular-nums"
                                    value={boards.maxOpenCards}
                                    onChange={(e) => setBoards({ ...boards, maxOpenCards: Math.max(1, parseInt(e.target.value) || 1) })} />
                                <p className="text-xs text-text-faint">At the limit an agent is told to close some cards first.</p>
                            </div>
                            <div className="space-y-1.5">
                                <Label htmlFor="gen-boards-shared" className={FIELD_LABEL}>Shared boards</Label>
                                <Input id="gen-boards-shared" type="number" min={0} className="w-40 font-medium tabular-nums"
                                    value={boards.maxShared}
                                    onChange={(e) => setBoards({ ...boards, maxShared: Math.max(0, parseInt(e.target.value) || 0) })} />
                                <p className="text-xs text-text-faint">0 means no limit.</p>
                            </div>
                        </div>
                    )}
                </div>
            </Section>

            <Section {...S} title="Agent teams">
                <div className={`space-y-1.5 ${FORM}`}>
                    <Label htmlFor="gen-team-at-once" className={FIELD_LABEL}>Members working at once, most</Label>
                    <Input id="gen-team-at-once" type="number" min={1} max={50} className="w-40 font-medium tabular-nums"
                        value={teamCeiling ?? ''} disabled={teamCeiling === null}
                        onChange={(e) => setTeamCeiling(Math.min(50, Math.max(1, parseInt(e.target.value) || 1)))} />
                    <p className="text-xs text-text-faint">
                        The most members any Team agent here runs side by side. Each Team agent sets its own number up
                        to this; extra members wait for a free slot. More at once costs no more, but providers&apos; rate
                        limits slow a large team down.
                    </p>
                </div>
            </Section>

            <Section {...S} title="Turn limit">
                <div className={`space-y-1.5 ${FORM}`}>
                    <Label className={FIELD_LABEL}>When an agent reaches its turn limit</Label>
                    <Select className="w-full max-w-sm" aria-label="When an agent reaches its turn limit"
                        aria-describedby="gen-turn-limit-help" disabled={turnLimitMode === null}
                        value={turnLimitMode === null ? undefined : (turnLimitMode || '__default__')}
                        onChange={(v) => setTurnLimitMode(v === '__default__' ? '' : v)}
                        options={WORKSPACE_TURN_LIMIT_CHOICES} />
                    <p id="gen-turn-limit-help" className="text-xs text-text-faint">
                        An agent that reaches its turn limit first gets one last turn to answer with what it has. If
                        it still hasn&apos;t finished, this decides what happens next, here and in Synapse AI. An
                        agent&apos;s own setting overrides it. Extra turns are billed as any turn is.
                    </p>
                </div>
            </Section>

            <Section {...S} title="Large response handling">
                <div className="space-y-4">
                    <SwitchRow label="Large response handling" checked={vaultEnabled} onChange={() => setVaultEnabled(!vaultEnabled)}>
                        When enabled, tool outputs exceeding the threshold are saved to a vault file instead of flooding the context.
                    </SwitchRow>
                    {vaultEnabled && (
                        <div className={`space-y-1.5 ${FORM}`}>
                            <Label htmlFor="gen-character-threshold" className={FIELD_LABEL}>Character threshold</Label>
                            <div className="flex items-center gap-3">
                                <Input id="gen-character-threshold"
                                    type="number"
                                    value={vaultDraft}
                                    onChange={(e) => setVaultDraft(e.target.value)}
                                    onBlur={() => {
                                        const v = Math.max(1, parseInt(vaultDraft) || 1);
                                        setVaultThreshold(v);
                                        setVaultDraft(String(v));
                                    }}
                                    className="w-40 font-medium tabular-nums"
                                    min={1}
                                />
                                <p className="text-xs text-text-faint">
                                    ≈ <span className="font-medium tabular-nums text-text">{Math.round(vaultThreshold / 4).toLocaleString()}</span> tokens
                                    <span className="ml-1">(at ~4 chars / token)</span>
                                </p>
                            </div>
                            <p className="text-xs text-text-faint">Responses longer than this many characters will be saved to a file.</p>
                        </div>
                    )}
                </div>
            </Section>

            <Section {...S} title="Auto context compaction">
                <div className="space-y-4">
                    <SwitchRow label="Auto context compaction" checked={autoCompactEnabled} onChange={() => setAutoCompactEnabled(!autoCompactEnabled)}>
                        When the accumulated context exceeds the threshold, the agent summarises everything so far to ~30% of its size and archives the original to the vault so nothing is lost.
                    </SwitchRow>
                    {autoCompactEnabled && (
                        <div className={`space-y-1.5 ${FORM}`}>
                            <Label htmlFor="gen-compaction-threshold-characters" className={FIELD_LABEL}>Compaction threshold (characters)</Label>
                            <div className="flex items-center gap-3">
                                <Input id="gen-compaction-threshold-characters"
                                    type="number"
                                    value={compactDraft}
                                    onChange={(e) => setCompactDraft(e.target.value)}
                                    onBlur={() => {
                                        const v = Math.max(10000, parseInt(compactDraft) || 10000);
                                        setAutoCompactThreshold(v);
                                        setCompactDraft(String(v));
                                    }}
                                    className="w-40 font-medium tabular-nums"
                                    min={10000}
                                />
                                <p className="text-xs text-text-faint">
                                    ≈ <span className="font-medium tabular-nums text-text">{Math.round(autoCompactThreshold / 4).toLocaleString()}</span> tokens
                                    <span className="ml-1">(at ~4 chars / token)</span>
                                </p>
                            </div>
                            <p className="text-xs text-text-faint">
                                When context exceeds this, it is compacted using the current model. The full original is archived to the vault.
                            </p>
                        </div>
                    )}
                </div>
            </Section>

            <Section {...S} title="Database write access">
                <div className="space-y-3">
                    <SwitchRow label="Database write access" checked={allowDbWrite} onChange={() => setAllowDbWrite(!allowDbWrite)}>
                        When disabled (default), agents are strictly limited to SELECT/SHOW/DESCRIBE queries.
                        When enabled, INSERT/UPDATE/DELETE and other write queries are allowed — but agents must always ask for confirmation before executing them.
                    </SwitchRow>
                    {allowDbWrite && (
                        <Note>
                            <p><strong>Write mode active.</strong> Agents MUST ask for explicit user confirmation before running any INSERT, UPDATE, DELETE, DROP, or CREATE queries. This is enforced in the system prompt.</p>
                        </Note>
                    )}
                </div>
            </Section>

            {/* Tool policy — the workspace floor. An agent sets its own per
                category and inherits the rest from here. */}
            <Section {...S} title="Tool policy">
                <div className="space-y-4">
                    <p className={`text-sm leading-relaxed text-text-muted ${FORM}`}>
                        What every agent may do with the tools it holds, decided by what a tool does rather than
                        by its name. An agent can be stricter or looser per category; anything it leaves alone
                        follows this. Allow everywhere is the shipped default, so nothing changes until you
                        change it here.
                    </p>
                    <div className={FORM}>
                        <ToolPolicyFields
                            value={toolPolicyDefaults}
                            onChange={setToolPolicyDefaults}
                        />
                    </div>
                </div>
            </Section>

            {/* Code Repository Indexing */}
            <Section {...S} title="Code repository indexing">
                <div className="space-y-3">
                    <SwitchRow label="Code repository indexing" checked={embedCode} onChange={handleEmbedToggle} disabled={embedChecking}>
                        When enabled, agents can semantically search your indexed code repositories using vector embeddings.
                        Requires PostgreSQL with the pgvector extension.
                    </SwitchRow>

                    {/* Checking state */}
                    {embedChecking && (
                        <p className="flex items-center gap-2 text-xs text-text-muted">
                            <Spinner size={14} />
                            Checking PostgreSQL setup…
                        </p>
                    )}

                    {/* No psql found */}
                    {embedCheckState?.issue === 'no_psql' && (
                        <Note>
                            <p><strong>PostgreSQL not found.</strong> Install it to enable code indexing.</p>
                            <ul className="space-y-0.5 text-text-muted">
                                <li><strong>Ubuntu/Debian:</strong> <code className={code}>sudo apt install postgresql postgresql-contrib</code></li>
                                <li><strong>macOS:</strong> <code className={code}>brew install postgresql</code></li>
                                <li><strong>Windows:</strong> Download from <span className="underline">postgresql.org/download/windows</span></li>
                            </ul>
                            <p className="text-text-muted">After installing, also install pgvector: <code className={code}>sudo apt install postgresql-pgvector</code> (Ubuntu) or <code className={code}>brew install pgvector</code> (macOS).</p>
                            <Button variant="secondary" size="sm" onClick={runEmbedCheck}>Check again</Button>
                        </Note>
                    )}

                    {/* Existing URL configured but connection is failing */}
                    {embedCheckState?.issue === 'existing_url_broken' && (
                        <div className={`${FORM} space-y-3 rounded-md border border-border p-4 text-xs`}>
                            <div className="space-y-1">
                                <p className="text-sm font-medium text-text">Existing connection is failing</p>
                                <p className="break-all font-code text-text-faint">{embedCheckState.detail}</p>
                                <p className="text-text-faint">Please provide new connection details to reconfigure.</p>
                            </div>
                            {dbFields}
                            {setupError && <p className="font-code text-2xs text-danger">{setupError}</p>}
                            <div className="flex items-center gap-2 pt-1">
                                <Button size="sm" onClick={handleSetupDb} loading={setupInProgress}>
                                    {setupInProgress ? 'Connecting…' : 'Save & connect'}
                                </Button>
                                <Button variant="ghost" size="sm" onClick={() => setEmbedCheckState(null)}>Cancel</Button>
                            </div>
                        </div>
                    )}

                    {/* pgvector missing */}
                    {embedCheckState?.issue === 'no_pgvector' && (
                        <Note>
                            <p><strong>pgvector extension not installed.</strong> PostgreSQL is running but the vector extension is missing.</p>
                            <p>Connect to your database and run:</p>
                            <code className="block rounded bg-surface-2 px-2 py-1.5 font-code text-text">CREATE EXTENSION vector;</code>
                            <p className="text-text-muted">Or install the OS package first: <code className={code}>sudo apt install postgresql-pgvector</code> (Ubuntu) / <code className={code}>brew install pgvector</code> (macOS), then run the SQL above.</p>
                            <Button variant="secondary" size="sm" onClick={runEmbedCheck}>Check again</Button>
                        </Note>
                    )}

                    {/* No DB URL configured — show fresh setup form */}
                    {embedCheckState?.issue === 'no_db' && (
                        <div className={`${FORM} space-y-3 rounded-md border border-border p-4 text-xs`}>
                            <div className="space-y-1">
                                <p className="text-sm font-medium text-text">Set up a PostgreSQL database for code indexing</p>
                                <p className="text-text-faint">No database configured yet. Enter your PostgreSQL credentials and we&apos;ll create the database and enable pgvector.</p>
                            </div>
                            {dbFields}
                            {setupError && <p className="font-code text-2xs text-danger">{setupError}</p>}
                            <div className="flex items-center gap-2 pt-1">
                                <Button size="sm" onClick={handleSetupDb} loading={setupInProgress}>
                                    {setupInProgress ? 'Creating…' : 'Create database'}
                                </Button>
                                <Button variant="ghost" size="sm" onClick={() => setEmbedCheckState(null)}>Cancel</Button>
                            </div>
                        </div>
                    )}

                    {/* Generic connection error */}
                    {embedCheckState?.issue === 'connection_error' && (
                        <Note tone="danger">
                            <p><strong>Check failed.</strong> {embedCheckState.detail}</p>
                            <Button variant="secondary" size="sm" onClick={runEmbedCheck}>Try again</Button>
                        </Note>
                    )}
                </div>
            </Section>

            {/* Bash Command Directories */}
            <Section {...S} title="Allowed directories">
                <div className={`space-y-3 ${FORM}`}>
                    <p className="text-sm leading-relaxed text-text-muted">
                        Directories the bash tool and filesystem MCP server can access.
                        Linked repos and vault are always included automatically.
                    </p>
                    {bashAllowedDirs.length > 0 && (
                        <ul className="divide-y divide-border border-y border-border">
                            {bashAllowedDirs.map((dir, i) => (
                                <li key={i} className="flex items-center justify-between gap-3 py-2">
                                    <span className="truncate font-code text-xs text-text">{dir}</span>
                                    <Button variant="ghost" size="sm" className="hover:text-danger"
                                        onClick={() => setBashAllowedDirs(bashAllowedDirs.filter((_, j) => j !== i))}>
                                        Remove
                                    </Button>
                                </li>
                            ))}
                        </ul>
                    )}
                    <div className="flex gap-2">
                        <Input
                            type="text"
                            value={newDir}
                            onChange={e => setNewDir(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter') addDir(); }}
                            placeholder="/path/to/directory"
                            aria-label="Directory to allow"
                            className="flex-1 font-code"
                        />
                        <Button variant="secondary" onClick={addDir} disabled={!newDir.trim()}>Add</Button>
                    </div>
                </div>
            </Section>

            {/* Transform Step Python Runtime — the server operator's, never a setting (F968) */}
            <Section {...S} title="Transform step Python runtime">
                <div className="space-y-3 text-sm text-text-muted">
                    {transformRuntime === 'host' ? (
                        <Note>
                            <p className="flex items-start gap-2">
                                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                                <span>
                                    <strong>This server runs Transform steps on the host</strong> (it was started with{' '}
                                    <code className="font-code text-text">SYNAPSE_TRANSFORM_RUNTIME=host</code>): outside the
                                    sandbox, as the server, able to read everything the server can, including stored keys,
                                    with the network. Only for a single-user machine you trust every workflow on.
                                </span>
                            </p>
                        </Note>
                    ) : (
                        <p>
                            Transform steps run in a Docker sandbox: 512 MB, one CPU, up to a minute, and no network.
                            Reach outside through a tool step and pass the result in.
                        </p>
                    )}
                    <p className="text-text-faint">
                        Set by whoever runs the server, not here: starting it with{' '}
                        <code className="font-code text-text">SYNAPSE_TRANSFORM_RUNTIME=host</code> runs Transform steps on
                        the host (full RAM, any GPU) — outside the sandbox, as the server, able to read everything the
                        server can, including stored keys. Only for a single-user machine you trust every workflow on.
                    </p>
                </div>
            </Section>

            {/* Login & Security */}
            <Section {...S} title="Login & security">
                <div className="space-y-4">
                    <div className={`flex items-start justify-between gap-6 ${FORM}`}>
                        <div>
                            <p className="text-sm font-medium text-text">Require login</p>
                            <p className="mt-0.5 text-sm text-text-muted">Protect Synapse with a username and password.</p>
                        </div>
                        <Switch
                            checked={loginEnabled || enabling}
                            label="Require login"
                            disabled={isLoginSaving}
                            className="mt-0.5"
                            onChange={() => {
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
                        />
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
                        <div className={`flex items-center justify-between gap-3 border-y border-border py-2.5 ${FORM}`}>
                            <p className="text-xs text-text-muted">
                                Login enabled for user: <span className="font-code text-text">{loginUsername}</span>
                            </p>
                            <Button variant="ghost" size="sm" onClick={() => { setShowLoginForm(true); setLoginFormError(''); }}>
                                Change password
                            </Button>
                        </div>
                    )}

                    {/* Credentials form */}
                    {(loginEnabled || enabling) && (showLoginForm || !loginUsername) && (
                        <div className={`space-y-3 rounded-md border border-border p-4 ${FORM}`}>
                            <p className="text-sm font-medium text-text">
                                {loginUsername ? 'Update credentials' : 'Set login credentials'}
                            </p>

                            <div className="space-y-1">
                                <Label htmlFor="gen-loginusername" className={FIELD_LABEL}>Username</Label>
                                <Input id="gen-loginusername"
                                    type="text"
                                    value={loginUsername}
                                    onChange={e => setLoginUsername(e.target.value)}
                                    autoComplete="off"
                                    placeholder="admin"
                                />
                            </div>

                            <div className="space-y-1">
                                <Label htmlFor="gen-login-password" className={FIELD_LABEL}>
                                    {loginUsername ? 'New password' : 'Password'}
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
                                <Label htmlFor="gen-loginconfirmpassword" className={FIELD_LABEL}>Confirm password</Label>
                                <Input id="gen-loginconfirmpassword"
                                    type="password"
                                    value={loginConfirmPassword}
                                    onChange={e => setLoginConfirmPassword(e.target.value)}
                                    autoComplete="new-password"
                                />
                            </div>

                            {loginFormError && (
                                <p className="text-xs text-danger">{loginFormError}</p>
                            )}

                            <div className="flex gap-2 pt-1">
                                <Button
                                    size="sm"
                                    loading={isLoginSaving}
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
                                >
                                    {isLoginSaving ? 'Saving…' : 'Save login settings'}
                                </Button>
                                <Button variant="ghost" size="sm" onClick={closeLoginForm}>Cancel</Button>
                            </div>

                            <p className="pt-1 text-xs text-text-faint">
                                Forgot your password? Run{' '}
                                <code className="rounded bg-surface-2 px-1 font-code text-2xs text-text-muted">synapse reset-password</code>
                                {' '}in your terminal.
                            </p>
                        </div>
                    )}
                </div>
            </Section>

            <SaveBar onSave={onSave} isSaving={isSaving} />
        </div>
    );
}

