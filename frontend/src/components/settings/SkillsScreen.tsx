"use client";

/*
 * /skills — the library of instruction packages agents load on demand.
 *
 * Two sources, one list. Library skills are created and edited here and live
 * in the store; disk skills were found under `.agents/skills/` and are
 * read-only — "Edit" on one creates a library copy under the same name, which
 * shadows it (the backend's collision rule, surfaced as a badge).
 *
 * The format is Claude Code's SKILL.md: a name, a one-line description that
 * is always in the agent's prompt, and a markdown body that costs nothing
 * until the agent calls load_skill.
 */
import { useCallback, useEffect, useState } from "react";
import { HardDrive, Library, Pencil, Plus, Trash } from "lucide-react";

import {
    Badge, Button, Card, CardBody, ConfirmDialog, EmptyState, Field, IconButton,
    Input, Modal, Textarea, Toast, useToast,
} from "@/components/ui";

type Skill = {
    name: string;
    description: string;
    source: "library" | "disk";
    shadowed?: boolean;
    path?: string;
};

type Draft = {
    name: string;
    description: string;
    body: string;
    /** Editing an existing library skill (name locked) vs creating a new one. */
    existing: boolean;
};

export function SkillsScreen() {
    const { toast, show, dismiss } = useToast();

    const [skills, setSkills] = useState<Skill[]>([]);
    const [loading, setLoading] = useState(true);
    const [draft, setDraft] = useState<Draft | null>(null);
    const [draftBusy, setDraftBusy] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

    const refresh = useCallback(async () => {
        try {
            const res = await fetch("/api/skills");
            setSkills(res.ok ? await res.json() : []);
        } catch {
            setSkills([]);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { refresh(); }, [refresh]);

    const openEditor = async (skill?: Skill) => {
        if (!skill) {
            setDraft({ name: "", description: "", body: "", existing: false });
            return;
        }
        // The list carries no bodies; fetch the full skill for the editor.
        try {
            const res = await fetch(`/api/skills/${encodeURIComponent(skill.name)}`);
            if (!res.ok) return show("Could not load the skill", "danger");
            const full = await res.json();
            setDraft({
                name: full.name,
                description: full.description,
                body: full.body || "",
                // A disk skill is edited by creating a library copy that shadows it.
                existing: skill.source === "library",
            });
        } catch {
            show("Could not load the skill", "danger");
        }
    };

    const saveDraft = async () => {
        if (!draft) return;
        if (!draft.name.trim()) return show("A skill needs a name", "warning");
        if (!draft.description.trim()) return show("A skill needs a description — it is what the agent sees before loading it", "warning");

        setDraftBusy(true);
        try {
            const payload = { name: draft.name, description: draft.description, body: draft.body };
            const res = draft.existing
                ? await fetch(`/api/skills/${encodeURIComponent(draft.name)}`, {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                })
                : await fetch("/api/skills", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
            if (!res.ok) {
                const detail = (await res.json().catch(() => null))?.detail;
                return show(detail || "Failed to save the skill", "danger");
            }
            setDraft(null);
            show("Skill saved");
            refresh();
        } catch {
            show("Error saving the skill", "danger");
        } finally {
            setDraftBusy(false);
        }
    };

    const deleteSkill = async () => {
        if (!confirmDelete) return;
        try {
            const res = await fetch(`/api/skills/${encodeURIComponent(confirmDelete)}`, { method: "DELETE" });
            if (!res.ok) {
                const detail = (await res.json().catch(() => null))?.detail;
                show(detail || "Could not delete the skill", "danger");
            } else {
                refresh();
            }
        } catch {
            show("Could not delete the skill", "danger");
        } finally {
            setConfirmDelete(null);
        }
    };

    return (
        <div className="space-y-4">
            <div className="flex justify-end">
                <Button onClick={() => openEditor()}>
                    <Plus size={14} aria-hidden /> New skill
                </Button>
            </div>

            {loading ? null : skills.length === 0 ? (
                <EmptyState title="No skills yet">
                    A skill is a named set of instructions an agent loads when the task calls
                    for it — its name and description ride the prompt, the body only loads on
                    demand. Create one here, or drop SKILL.md folders into{" "}
                    <span className="font-mono">.agents/skills/</span> next to the install.
                </EmptyState>
            ) : (
                <div className="grid gap-3 sm:grid-cols-2">
                    {skills.map((skill) => (
                        <Card key={`${skill.source}:${skill.name}`}>
                            <CardBody className="space-y-2">
                                <div className="flex items-start justify-between gap-2">
                                    <div className="min-w-0">
                                        <div className="flex items-center gap-2">
                                            <span className="font-mono text-sm truncate">{skill.name}</span>
                                            {skill.source === "disk" ? (
                                                <Badge tone="neutral"><HardDrive size={10} aria-hidden /> Disk</Badge>
                                            ) : (
                                                <Badge tone="accent"><Library size={10} aria-hidden /> Library</Badge>
                                            )}
                                            {skill.shadowed && (
                                                <Badge tone="warning">Shadowed by a library skill</Badge>
                                            )}
                                        </div>
                                        <p className="mt-1 text-xs text-zinc-400 line-clamp-2">{skill.description}</p>
                                        {skill.path && (
                                            <p className="mt-1 text-[10px] text-zinc-600 font-mono truncate" title={skill.path}>{skill.path}</p>
                                        )}
                                    </div>
                                    <div className="flex shrink-0 items-center gap-1">
                                        <IconButton
                                            label={skill.source === "disk" ? "Edit a library copy (shadows the disk file)" : "Edit"}
                                            icon={Pencil}
                                            onClick={() => openEditor(skill)}
                                        />
                                        {skill.source === "library" && (
                                            <IconButton label="Delete" icon={Trash} onClick={() => setConfirmDelete(skill.name)} />
                                        )}
                                    </div>
                                </div>
                            </CardBody>
                        </Card>
                    ))}
                </div>
            )}

            <Modal
                open={!!draft}
                onClose={() => setDraft(null)}
                title={draft?.existing ? `Edit skill: ${draft.name}` : "New skill"}
                description="Claude Code's SKILL.md shape: the description is always in the agent's prompt; the body loads only when the agent calls load_skill."
                size="lg"
                footer={
                    <>
                        <Button variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
                        <Button onClick={saveDraft} disabled={draftBusy}>
                            {draftBusy ? "Saving…" : "Save skill"}
                        </Button>
                    </>
                }
            >
                {draft && (
                    <div className="space-y-3">
                        <Field
                            label="Name"
                            required
                            hint={draft.existing ? "Names cannot change in place — agents select skills by name." : <>Lower-case, hyphens for spaces — e.g. <span className="font-mono">quarterly-report-format</span></>}
                        >
                            <Input
                                value={draft.name}
                                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                                disabled={draft.existing}
                                placeholder="my-skill-name"
                            />
                        </Field>
                        <Field
                            label="Description"
                            required
                            hint="One line. The agent reads this every turn to decide whether the skill applies — write it for that decision."
                        >
                            <Input
                                value={draft.description}
                                onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                                placeholder="When and how to format the weekly market summary"
                            />
                        </Field>
                        <Field
                            label="Instructions (markdown)"
                            hint="Loaded into the agent's context only when it calls load_skill."
                        >
                            <Textarea
                                value={draft.body}
                                onChange={(e) => setDraft({ ...draft, body: e.target.value })}
                                rows={14}
                                className="font-mono text-xs"
                                placeholder={"# My skill\n\nStep-by-step instructions the agent follows once loaded…"}
                            />
                        </Field>
                    </div>
                )}
            </Modal>

            <ConfirmDialog
                open={!!confirmDelete}
                onClose={() => setConfirmDelete(null)}
                onConfirm={deleteSkill}
                title="Delete skill?"
                description={
                    <>
                        <span className="font-mono">{confirmDelete}</span> will stop being offered to
                        agents. Agents that selected it keep the name and skip it at run time.
                    </>
                }
            />

            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </div>
    );
}
