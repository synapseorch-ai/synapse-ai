/* eslint-disable @typescript-eslint/no-explicit-any */
import { Database } from 'lucide-react';
import { Select } from '@/components/ui';

/** The Select shows an empty value as "Select…", so the default has a value of its own. */
const WORKSPACE_DEFAULT = '__workspace_default__';

interface DataLabTabProps {
    dlTopic: string; setDlTopic: (v: string) => void;
    dlCount: number; setDlCount: (v: number) => void;
    /** '' = the workspace's default model. */
    dlModel: string; setDlModel: (v: string) => void;
    /** Every model the workspace can reach, and the default one. */
    modelOptions: string[]; defaultModel: string;
    dlSystemPrompt: string; setDlSystemPrompt: (v: string) => void;
    dlEdgeCases: string; setDlEdgeCases: (v: string) => void;
    dlStatus: any;
    dlDatasets: any[];
    onGenerate: () => void;
}

export const DataLabTab = ({
    dlTopic, setDlTopic, dlCount, setDlCount,
    dlModel, setDlModel, modelOptions, defaultModel, dlSystemPrompt, setDlSystemPrompt,
    dlEdgeCases, setDlEdgeCases, dlStatus, dlDatasets, onGenerate
}: DataLabTabProps) => (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-8 h-full">
        <div className="space-y-6 overflow-y-auto pr-2">
            <div className="space-y-4">
                <div className="space-y-1">
                    <label htmlFor="datalab-domain-topic" className="text-[10px] uppercase font-bold text-text-faint">Domain / Topic</label>
                    <input
                        id="datalab-domain-topic" type="text" value={dlTopic} onChange={e => setDlTopic(e.target.value)}
                        className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none rounded-md"
                        placeholder="e.g. Medical Assistant, Python Coding Tutor" />
                </div>

                <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-1">
                        <label htmlFor="datalab-count" className="text-[10px] uppercase font-bold text-text-faint">Count</label>
                        <input
                            id="datalab-count" type="number" value={dlCount} onChange={e => setDlCount(parseInt(e.target.value))}
                            className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none rounded-md"
                            min={1} max={100} />
                    </div>
                    <div className="space-y-1">
                        <label className="text-[10px] uppercase font-bold text-text-faint">Model</label>
                        {/* Any model the workspace reaches, through the engine's
                            own call — the key stays on the server (F210). */}
                        <Select
                            value={dlModel || WORKSPACE_DEFAULT}
                            onChange={v => setDlModel(v === WORKSPACE_DEFAULT ? '' : v)}
                            aria-label="Model"
                            options={[
                                { value: WORKSPACE_DEFAULT, label: `Workspace default${defaultModel ? ` (${defaultModel})` : ''}` },
                                ...modelOptions.filter(m => m !== '').map(m => ({ value: m, label: m })),
                            ]}
                        />
                    </div>
                </div>

                <div className="space-y-1">
                    <label htmlFor="datalab-target-persona-system-prompt" className="text-[10px] uppercase font-bold text-text-faint">Target Persona (System Prompt)</label>
                    <textarea
                        id="datalab-target-persona-system-prompt" value={dlSystemPrompt} onChange={e => setDlSystemPrompt(e.target.value)}
                        className="w-full h-24 bg-zinc-900 border border-zinc-800 p-3 text-xs font-mono text-zinc-300 focus:border-white focus:outline-none resize-none rounded-md" />
                </div>

                <div className="space-y-1">
                    <label htmlFor="datalab-edge-cases-constraints" className="text-[10px] uppercase font-bold text-text-faint">Edge Cases & Constraints</label>
                    <textarea
                        id="datalab-edge-cases-constraints" value={dlEdgeCases} onChange={e => setDlEdgeCases(e.target.value)}
                        className="w-full h-24 bg-zinc-900 border border-zinc-800 p-3 text-xs font-mono text-zinc-300 focus:border-white focus:outline-none resize-none rounded-md"
                        placeholder="e.g. 'If user asks for illegal advice, politely refuse.' or 'Always include code comments.'" />
                </div>

                <button onClick={onGenerate} disabled={dlStatus?.status === 'generating'}
                    className="w-full py-4 bg-accent text-accent-fg font-bold text-sm tracking-uppercase hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed">
                    {dlStatus?.status === 'generating' ? 'GENERATING...' : 'START GENERATION JOB'}
                </button>
            </div>
        </div>

        <div className="bg-zinc-950 border border-zinc-800 p-6 flex flex-col h-full rounded-md">
            <h3 className="text-sm font-bold text-zinc-400 mb-4 flex items-center justify-between">
                <span>DATASETS</span>
                {dlStatus?.status === 'generating' && (
                    <span className="text-green-500 text-xs animate-pulse">Running: {dlStatus.completed}/{dlStatus.total}</span>
                )}
                {dlStatus?.status === 'failed' && (
                    <span className="text-red-500 text-xs">Failed: {dlStatus.error}</span>
                )}
                {dlStatus?.status === 'completed' && dlStatus.error && (
                    <span className="text-amber-500 text-xs">Done with errors: {dlStatus.error}</span>
                )}
            </h3>

            <div className="flex-1 overflow-y-auto space-y-2">
                {dlDatasets.length === 0 ? (
                    <div className="text-center py-10 text-text-faint text-xs italic">No datasets generated yet.</div>
                ) : (
                    dlDatasets.map((ds: any) => (
                        <div key={ds.filename} className="p-3 bg-black border border-zinc-800 flex justify-between items-center group hover:border-zinc-600 rounded-md">
                            <div className="flex-1 min-w-0">
                                <div className="text-xs text-white font-mono truncate mb-1">{ds.filename}</div>
                                <div className="text-[10px] text-text-faint flex gap-2">
                                    <span>{(ds.size / 1024).toFixed(1)} KB</span>
                                    <span>•</span>
                                    <span>{new Date(ds.created).toLocaleDateString()}</span>
                                </div>
                            </div>
                            <Database className="h-4 w-4 text-text-faint group-hover:text-zinc-400" />
                        </div>
                    ))
                )}
            </div>
        </div>
    </div>
);
