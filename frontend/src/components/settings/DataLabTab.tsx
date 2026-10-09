/* eslint-disable @typescript-eslint/no-explicit-any */
import { Database } from 'lucide-react';
import { Button, EmptyState, Input, Section, Select, Textarea } from '@/components/ui';
import { FIELD_LABEL, FORM } from './form';

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
    <div className="space-y-10">
        <div className={`${FORM} space-y-4`}>
            <div className="space-y-1.5">
                <label htmlFor="datalab-domain-topic" className={FIELD_LABEL}>Domain / topic</label>
                <Input
                    id="datalab-domain-topic" type="text" value={dlTopic} onChange={e => setDlTopic(e.target.value)}
                    placeholder="e.g. Medical Assistant, Python Coding Tutor" />
            </div>

            <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1.5">
                    <label htmlFor="datalab-count" className={FIELD_LABEL}>Count</label>
                    <Input
                        id="datalab-count" type="number" value={dlCount} onChange={e => setDlCount(parseInt(e.target.value))}
                        min={1} max={100} />
                </div>
                <div className="space-y-1.5">
                    <label className={FIELD_LABEL}>Model</label>
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

            <div className="space-y-1.5">
                <label htmlFor="datalab-target-persona-system-prompt" className={FIELD_LABEL}>Target persona (system prompt)</label>
                <Textarea
                    id="datalab-target-persona-system-prompt" value={dlSystemPrompt} onChange={e => setDlSystemPrompt(e.target.value)}
                    className="h-24 resize-none font-code text-xs" />
            </div>

            <div className="space-y-1.5">
                <label htmlFor="datalab-edge-cases-constraints" className={FIELD_LABEL}>Edge cases & constraints</label>
                <Textarea
                    id="datalab-edge-cases-constraints" value={dlEdgeCases} onChange={e => setDlEdgeCases(e.target.value)}
                    className="h-24 resize-none font-code text-xs"
                    placeholder="e.g. 'If user asks for illegal advice, politely refuse.' or 'Always include code comments.'" />
            </div>

            <Button onClick={onGenerate} disabled={dlStatus?.status === 'generating'}>
                {dlStatus?.status === 'generating' ? 'Generating...' : 'Start generation job'}
            </Button>
        </div>

        <Section
            variant="plain"
            collapsible={false}
            title="Datasets"
            actions={
                <>
                    {dlStatus?.status === 'generating' && (
                        <span className="flex items-center gap-1.5 text-xs text-text-muted">
                            <span className="size-1.5 rounded-full bg-success animate-pulse" />
                            Running: {dlStatus.completed}/{dlStatus.total}
                        </span>
                    )}
                    {dlStatus?.status === 'failed' && (
                        <span className="text-danger text-xs">Failed: {dlStatus.error}</span>
                    )}
                    {dlStatus?.status === 'completed' && dlStatus.error && (
                        <span className="flex items-center gap-1.5 text-xs text-text-muted">
                            <span className="size-1.5 rounded-full bg-warning" />
                            Done with errors: {dlStatus.error}
                        </span>
                    )}
                </>
            }
        >
            {dlDatasets.length === 0 ? (
                <EmptyState title="No datasets generated yet." />
            ) : (
                <div className="divide-y divide-border rounded-md border border-border">
                    {dlDatasets.map((ds: any) => (
                        <div key={ds.filename} className="flex justify-between items-center gap-3 px-4 py-3 group hover:bg-row-hover">
                            <div className="flex-1 min-w-0">
                                <div className="text-sm text-text font-code truncate mb-0.5">{ds.filename}</div>
                                <div className="text-xs text-text-faint flex gap-2">
                                    <span>{(ds.size / 1024).toFixed(1)} KB</span>
                                    <span>•</span>
                                    <span>{new Date(ds.created).toLocaleDateString()}</span>
                                </div>
                            </div>
                            <Database className="h-4 w-4 text-text-faint group-hover:text-text-muted" />
                        </div>
                    ))}
                </div>
            )}
        </Section>
    </div>
);
