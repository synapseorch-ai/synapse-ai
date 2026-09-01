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
import { Download, Eye, HardDrive, Library, Pencil, PencilLine, Plus, Trash } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

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
    const [draftTab, setDraftTab] = useState<"edit" | "preview">("edit");
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

    // Install-from-CLI state. `cliAvailable` is asked once: a machine without
    // Node should not be offered a button that can only fail.
    const [cliAvailable, setCliAvailable] = useState(false);
    const [installOpen, setInstallOpen] = useState(false);
    const [installPackage, setInstallPackage] = useState("");
    const [installBusy, setInstallBusy] = useState(false);
    const [installLog, setInstallLog] = useState("");

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

    useEffect(() => {
        fetch("/api/skills-cli")
            .then((res) => (res.ok ? res.json() : { available: false }))
            .then((data) => setCliAvailable(!!data.available))
            .catch(() => setCliAvailable(false));
    }, []);

    const install = async () => {
        const pkg = installPackage.trim();
        if (!pkg) return show("Name a package to install", "warning");
        setInstallBusy(true);
        setInstallLog("");
        try {
            const res = await fetch("/api/skills/install", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ package: pkg }),
            });
            const data = await res.json().catch(() => null);
            if (!res.ok) {
                // The CLI's own output is the useful part of a failure, so it
                // goes on screen rather than into a toast that scrolls away.
                setInstallLog(String(data?.detail ?? "The install failed."));
                return;
            }
            const names: string[] = data?.installed ?? [];
            if (names.length === 0) {
                setInstallLog(
                    `${pkg} installed, but no new skill appeared — it may already be installed.\n\n` +
                    String(data?.output ?? ""),
                );
            } else {
                show(`Installed ${names.join(", ")}`);
                setInstallOpen(false);
                setInstallPackage("");
            }
            refresh();
        } catch {
            setInstallLog("The install could not be started.");
        } finally {
            setInstallBusy(false);
        }
    };

    const openEditor = async (skill?: Skill) => {
        if (!skill) {
            setDraftTab("edit");
            setDraft({ name: "", description: "", body: "", existing: false });
            return;
        }
        // An existing skill opens on Preview: the common reason to open one is
        // to read what it tells the agent, not to rewrite it.
        setDraftTab("preview");
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
            <div className="flex justify-end gap-2">
                {cliAvailable && (
                    <Button
                        variant="secondary"
                        onClick={() => { setInstallLog(""); setInstallOpen(true); }}
                    >
                        <Download size={14} aria-hidden /> Install from skills.sh
                    </Button>
                )}
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
                        <div className="flex items-center justify-between">
                            <span className="text-xs font-medium text-text">Instructions (markdown)</span>
                            <div className="flex overflow-hidden rounded-md border border-border">
                                {(["edit", "preview"] as const).map((tab) => {
                                    const TabIcon = tab === "edit" ? PencilLine : Eye;
                                    return (
                                        <button
                                            key={tab}
                                            type="button"
                                            onClick={() => setDraftTab(tab)}
                                            aria-pressed={draftTab === tab}
                                            className={`flex items-center gap-1 px-2.5 py-1 text-xs transition-colors ${
                                                draftTab === tab
                                                    ? "bg-accent text-accent-fg"
                                                    : "bg-surface text-text-muted hover:bg-surface-2"
                                            }`}
                                        >
                                            <TabIcon size={11} aria-hidden />
                                            {tab === "edit" ? "Edit" : "Preview"}
                                        </button>
                                    );
                                })}
                            </div>
                        </div>
                        {draftTab === "preview" ? (
                            <div className="max-h-80 overflow-y-auto rounded-md border border-border bg-bg px-3 py-2">
                                {draft.body.trim() ? (
                                    /* Explicit overrides rather than a `prose` class: there is no
                                       typography plugin here, and preflight resets headings and
                                       lists to nothing — which is exactly what a skill body is
                                       made of. Same approach the run log already uses. */
                                    <div className="text-sm text-text-muted">
                                        <ReactMarkdown
                                            remarkPlugins={[remarkGfm]}
                                            components={{
                                                h1: ({ children }) => <h1 className="mb-1.5 mt-3 text-base font-semibold text-text first:mt-0">{children}</h1>,
                                                h2: ({ children }) => <h2 className="mb-1 mt-3 text-sm font-semibold text-text first:mt-0">{children}</h2>,
                                                h3: ({ children }) => <h3 className="mb-1 mt-2.5 text-xs font-semibold uppercase tracking-wide text-text-muted first:mt-0">{children}</h3>,
                                                p: ({ children }) => <p className="mb-2 leading-relaxed">{children}</p>,
                                                ul: ({ children }) => <ul className="mb-2 list-disc space-y-0.5 pl-5">{children}</ul>,
                                                ol: ({ children }) => <ol className="mb-2 list-decimal space-y-0.5 pl-5">{children}</ol>,
                                                li: ({ children }) => <li className="leading-relaxed">{children}</li>,
                                                strong: ({ children }) => <strong className="font-semibold text-text">{children}</strong>,
                                                a: ({ href, children }) => <a href={href} className="text-accent underline" target="_blank" rel="noreferrer">{children}</a>,
                                                code: ({ children }) => <code className="rounded-md bg-surface-2 px-1 py-0.5 font-code text-[11px] text-text">{children}</code>,
                                                pre: ({ children }) => <pre className="mb-2 overflow-x-auto rounded-md bg-surface-2 p-2 font-code text-[11px]">{children}</pre>,
                                                blockquote: ({ children }) => <blockquote className="mb-2 border-l-2 border-border pl-3 italic">{children}</blockquote>,
                                                hr: () => <hr className="my-3 border-border" />,
                                                table: ({ children }) => <div className="mb-2 overflow-x-auto"><table className="w-full text-xs">{children}</table></div>,
                                                th: ({ children }) => <th className="border-b border-border px-2 py-1 text-left font-semibold text-text">{children}</th>,
                                                td: ({ children }) => <td className="border-b border-border/50 px-2 py-1">{children}</td>,
                                            }}
                                        >{draft.body}</ReactMarkdown>
                                    </div>
                                ) : (
                                    <p className="text-xs text-text-faint">
                                        Nothing to preview yet — switch to Edit and write the instructions.
                                    </p>
                                )}
                            </div>
                        ) : (
                        <Field
                            label=""
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
                        )}
                    </div>
                )}
            </Modal>

            <Modal
                open={installOpen}
                onClose={() => setInstallOpen(false)}
                title="Install from skills.sh"
                description="Runs the Skills CLI (npx skills add) and installs into your user skills folder, ~/.agents/skills."
                footer={
                    <>
                        <Button variant="ghost" onClick={() => setInstallOpen(false)}>Cancel</Button>
                        <Button onClick={install} disabled={installBusy}>
                            {installBusy ? "Installing…" : "Install"}
                        </Button>
                    </>
                }
            >
                <div className="space-y-3">
                    <Field
                        label="Package"
                        required
                        hint={<>From <span className="font-mono">skills.sh</span> — e.g. <span className="font-mono">anthropics/skills</span> or <span className="font-mono">vercel-labs/agent-skills@react-best-practices</span></>}
                    >
                        <Input
                            value={installPackage}
                            onChange={(e) => setInstallPackage(e.target.value)}
                            onKeyDown={(e) => { if (e.key === "Enter" && !installBusy) install(); }}
                            placeholder="owner/repo@skill"
                            disabled={installBusy}
                        />
                    </Field>
                    <p className="text-2xs text-text-faint">
                        This downloads and installs third-party instructions on this machine. Install
                        skills you trust — an agent follows them as written.
                    </p>
                    {installLog && (
                        <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg px-3 py-2 font-mono text-2xs text-text-muted">
                            {installLog}
                        </pre>
                    )}
                </div>
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
