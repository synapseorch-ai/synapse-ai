'use client';
/**
 * "See how I'm built" — the builder showing its own working.
 *
 * The point is not decoration: the builder is an ordinary orchestration with
 * ordinary sub-agents, and saying so is the strongest argument the product has
 * for what the canvas can do. The legend is rendered from the definition that
 * actually ships, so it cannot drift into describing a builder we no longer
 * have. The picture above it is that orchestration on a read-only canvas —
 * drawn from the same definition, so it cannot go stale the way the
 * screenshot it replaced did.
 */
import { useEffect, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { ArrowRight } from 'lucide-react';

import { Badge, Button, Modal, Spinner } from '@/components/ui';
import { WorkflowCanvas } from '@/components/orchestration/WorkflowCanvas';
import type { Orchestration } from '@/types/orchestration';

interface Legend {
    name: string;
    description: string;
    step_count: number;
    entry_step: { id: string; name: string; type: string };
    routes: { label: string; to: string; description: string }[];
    human_gates: { id: string; name: string; asks: string }[];
    agents: { role: string; name: string; description: string; step: string; tools: string[] }[];
    step_types: string[];
    canvas: { orchestration: Orchestration; agents: { id: string; name: string }[] };
}

export function HowImBuilt({
    open,
    onClose,
    onDuplicated,
}: {
    open: boolean;
    onClose: () => void;
    onDuplicated: (orchestrationId: string) => void;
}) {
    const [legend, setLegend] = useState<Legend | null>(null);
    const [duplicating, setDuplicating] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!open || legend) return;
        (async () => {
            try {
                const response = await fetch('/api/builder/legend');
                if (response.ok) setLegend(await response.json());
            } catch {
                /* the copy button still works */
            }
        })();
    }, [open, legend]);

    const duplicate = async () => {
        setDuplicating(true);
        setError(null);
        try {
            const response = await fetch('/api/builder/duplicate', { method: 'POST' });
            if (!response.ok) throw new Error('The copy could not be created.');
            const result = await response.json();
            onDuplicated(result.orchestration_id);
            onClose();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setDuplicating(false);
        }
    };

    return (
        <Modal
            open={open}
            onClose={onClose}
            size="lg"
            title="How I'm built"
            description="I am an ordinary SynapseOrch orchestration. Every step type you see here is available on your canvas."
            footer={
                <div className="flex w-full items-center justify-between gap-3">
                    <p className="text-xs text-text-faint">
                        {legend ? `${legend.step_count} steps · ${legend.agents.length} sub-agents` : ''}
                    </p>
                    <div className="flex items-center gap-2">
                        {error && <span className="text-xs text-danger">{error}</span>}
                        <Button variant="secondary" onClick={onClose}>Close</Button>
                        <Button onClick={duplicate} disabled={duplicating}>
                            {duplicating ? <Spinner size={16} /> : null}
                            Duplicate to my workspace
                        </Button>
                    </div>
                </div>
            }
        >
            <div className="space-y-5">
                {legend && (
                    <div
                        role="group"
                        aria-label={`The ${legend.name} orchestration on the canvas`}
                        className="h-80 overflow-hidden rounded-md border border-border bg-bg"
                    >
                        <ReactFlowProvider>
                            <WorkflowCanvas
                                orchestration={legend.canvas.orchestration}
                                agents={legend.canvas.agents}
                                readOnly
                            />
                        </ReactFlowProvider>
                    </div>
                )}

                {legend && (
                    <div className="space-y-4 text-sm">
                        <section className="space-y-1.5">
                            <h3 className="font-code text-[11px] uppercase tracking-widest text-text-faint">
                                Where it starts
                            </h3>
                            <p className="text-text-muted">
                                <span className="text-text">{legend.entry_step.name}</span> — a{' '}
                                <span className="font-code text-accent">{legend.entry_step.type}</span> step that
                                reads what you asked for and picks a path.
                            </p>
                            <ul className="space-y-1 pt-1">
                                {legend.routes.map((route) => (
                                    <li key={route.label} className="flex items-start gap-2 text-text-muted">
                                        <ArrowRight size={13} className="mt-0.5 shrink-0 text-text-faint" aria-hidden />
                                        <span>
                                            <span className="font-code text-accent">{route.label}</span> → {route.to}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                        </section>

                        <section className="space-y-1.5">
                            <h3 className="font-code text-[11px] uppercase tracking-widest text-text-faint">
                                Where it stops for you
                            </h3>
                            <ul className="space-y-1">
                                {legend.human_gates.map((gate) => (
                                    <li key={gate.id} className="text-text-muted">
                                        <span className="text-text">{gate.name}</span>
                                        {gate.asks ? ` — asks: “${gate.asks}”` : ''}
                                    </li>
                                ))}
                            </ul>
                        </section>

                        <section className="space-y-2">
                            <h3 className="font-code text-[11px] uppercase tracking-widest text-text-faint">
                                The sub-agents
                            </h3>
                            <ul className="space-y-2.5">
                                {legend.agents.map((agent) => (
                                    <li key={agent.role} className="space-y-1">
                                        <p className="text-text">
                                            {agent.name}{' '}
                                            <span className="text-text-faint">· runs at {agent.step}</span>
                                        </p>
                                        <p className="text-xs text-text-muted">{agent.description}</p>
                                        <div className="flex flex-wrap gap-1 pt-0.5">
                                            {agent.tools.slice(0, 8).map((tool) => (
                                                <Badge key={tool} tone="neutral">{tool}</Badge>
                                            ))}
                                            {agent.tools.length > 8 && (
                                                <span className="text-[11px] text-text-faint">
                                                    +{agent.tools.length - 8} more
                                                </span>
                                            )}
                                        </div>
                                    </li>
                                ))}
                            </ul>
                        </section>

                        <section className="space-y-1.5">
                            <h3 className="font-code text-[11px] uppercase tracking-widest text-text-faint">
                                Step types it uses
                            </h3>
                            <div className="flex flex-wrap gap-1">
                                {legend.step_types.map((type) => (
                                    <Badge key={type} tone="accent">{type}</Badge>
                                ))}
                            </div>
                        </section>
                    </div>
                )}
            </div>
        </Modal>
    );
}
