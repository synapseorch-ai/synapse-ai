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
 *
 * The list beside the open skill (M2h-2, cloud's model): the first skill opens,
 * on Preview, so the pane is never empty; New skill opens a blank one in its
 * place. "Used by" is read from the agents' own skill lists.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RESOURCE_SAVED_EVENT } from '@/components/app/BuilderProvider';
import { AppScreen } from '@/components/app/AppScreen';
import { Download, HardDrive, Library, Pencil, Plus, Trash } from "lucide-react";

import { DetailHead, LIST_DETAIL, ListRow, showDetail } from "@/components/run";
import {
    Badge, Button, ConfirmDialog, EmptyState, Field, IconButton,
    Input, MarkdownEditor, Modal, SearchInput, Toast, useToast,
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

/** What is open: a listed skill by `source:name`, or a new one. */
type Open = { key: string } | { key: "new" };

const keyOf = (s: Skill) => `${s.source}:${s.name}`;
const BLANK: Draft = { name: "", description: "", body: "", existing: false };

export function SkillsScreen() {
    const { toast, show, dismiss } = useToast();

    const [skills, setSkills] = useState<Skill[]>([]);
    const [agents, setAgents] = useState<{ name: string; skills?: string[] }[]>([]);
    const [loading, setLoading] = useState(true);
    const [query, setQuery] = useState("");
    const [picked, setPicked] = useState<Open | null>(null);
    // The open skill's editor, keyed by what it was read for, so a stale read
    // never lands on another skill (and nothing resets inside an effect).
    const [read, setRead] = useState<{ key: string; draft: Draft; saved: Draft } | null>(null);
    const [draftBusy, setDraftBusy] = useState(false);
    const [draftTab, setDraftTab] = useState<"edit" | "preview">("preview");
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
    const detailRef = useRef<HTMLElement>(null);

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

    useEffect(() => {
        refresh();
        // The AI builder reaches this from any screen now, so the
        // list refreshes on the event rather than on a reload.
        window.addEventListener(RESOURCE_SAVED_EVENT, refresh);
        return () => window.removeEventListener(RESOURCE_SAVED_EVENT, refresh);
    }, [refresh]);

    useEffect(() => {
        fetch("/api/skills-cli")
            .then((res) => (res.ok ? res.json() : { available: false }))
            .then((data) => setCliAvailable(!!data.available))
            .catch(() => setCliAvailable(false));
        fetch("/api/agents")
            .then((res) => (res.ok ? res.json() : []))
            .then((data) => setAgents(Array.isArray(data) ? data : []))
            .catch(() => setAgents([]));
    }, []);

    const shown = useMemo(() => {
        const q = query.trim().toLowerCase();
        if (!q) return skills;
        return skills.filter((s) => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q));
    }, [skills, query]);

    // UX1: the detail never opens empty. A picked skill that is gone falls back to the first.
    const openSkill = picked?.key === "new" ? null : skills.find((s) => keyOf(s) === picked?.key) ?? skills[0] ?? null;
    const openKey = picked?.key === "new" ? "new" : openSkill ? keyOf(openSkill) : null;

    // Read the open skill's body (the list carries none).
    useEffect(() => {
        if (!openSkill) return;
        const key = keyOf(openSkill);
        if (read?.key === key) return;
        const ctl = new AbortController();
        fetch(`/api/skills/${encodeURIComponent(openSkill.name)}`, { signal: ctl.signal })
            .then(async (res) => {
                if (!res.ok) return show("Could not load the skill", "danger");
                const full = await res.json();
                const draft: Draft = {
                    name: full.name,
                    description: full.description,
                    body: full.body || "",
                    // A disk skill is edited by creating a library copy that shadows it.
                    existing: openSkill.source === "library",
                };
                setRead({ key, draft, saved: draft });
            })
            .catch((e) => { if (e?.name !== "AbortError") show("Could not load the skill", "danger"); });
        return () => ctl.abort();
    }, [openSkill, read?.key, show]);

    const draft = openKey === "new"
        ? (read?.key === "new" ? read.draft : BLANK)
        : read && read.key === openKey ? read.draft : null;
    const setDraft = (next: Draft) => setRead((r) => ({ key: openKey ?? "new", draft: next, saved: r?.key === openKey ? r.saved : next }));
    const dirty = !!read && read.key === openKey && JSON.stringify(read.draft) !== JSON.stringify(read.saved);

    const open = (next: Open, tab: "edit" | "preview") => {
        setPicked(next);
        // An existing skill opens on Preview: the common reason to open one is
        // to read what it tells the agent, not to rewrite it.
        setDraftTab(tab);
        if (next.key === "new") setRead({ key: "new", draft: BLANK, saved: BLANK });
        showDetail(detailRef.current);
    };

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
            // Saved means it is a library skill now: open that one, read again.
            const key = `library:${draft.name}`;
            setPicked({ key });
            setRead(null);
            setDraftTab("preview");
            show("Skill saved");
            refresh();
        } catch {
            show("Error saving the skill", "danger");
        } finally {
            setDraftBusy(false);
        }
    };

    const cancel = () => {
        // A new skill is dropped; a saved one gets its saved words back.
        if (openKey === "new") { setPicked(null); setRead(null); }
        else if (read && read.key === openKey) setRead({ ...read, draft: read.saved });
        setDraftTab("preview");
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

    const usedBy = (name: string) => agents.filter((a) => a.skills?.includes(name)).map((a) => a.name);
    const editLabel = (s: Skill) => (s.source === "disk" ? "Edit a library copy (shadows the disk file)" : "Edit");

    const actions = (
        <>
            {cliAvailable && (
                <Button
                    variant="secondary"
                    onClick={() => { setInstallLog(""); setInstallOpen(true); }}
                >
                    <Download size={14} aria-hidden /> Install from skills.sh
                </Button>
            )}
            <Button onClick={() => open({ key: "new" }, "edit")}>
                <Plus size={14} aria-hidden /> New skill
            </Button>
        </>
    );

    const users = openSkill ? usedBy(openSkill.name) : [];

    return (
        <AppScreen id="skills" width="wide" actions={actions}>
            {loading ? null : skills.length === 0 && openKey !== "new" ? (
                <EmptyState title="No skills yet">
                    A skill is a named set of instructions an agent loads when the task calls
                    for it — its name and description ride the prompt, the body only loads on
                    demand. Create one here, or drop SKILL.md folders into{" "}
                    <span className="font-code">.agents/skills/</span> next to the install.
                </EmptyState>
            ) : (
                <div className={LIST_DETAIL}>
                    <nav aria-label="Skills" className="grid gap-3">
                        <SearchInput value={query} onChange={setQuery} placeholder="Search skills…" className="max-w-none" />
                        {shown.length === 0 ? (
                            <p className="m-0 px-2.5 py-2 text-sm text-text-faint">
                                {skills.length === 0 ? "No skills yet." : "Nothing matches that."}
                            </p>
                        ) : (
                            <ul className="m-0 list-none border-t border-border p-0">
                                {shown.map((skill) => (
                                    <ListRow
                                        key={keyOf(skill)}
                                        data={{ "data-skill-row": skill.name }}
                                        title={skill.name}
                                        titleClassName="font-code"
                                        sub={skill.description}
                                        meta={skill.source === "disk" ? (skill.shadowed ? "Disk · shadowed" : "Disk") : "Library"}
                                        current={openKey === keyOf(skill)}
                                        onClick={() => open({ key: keyOf(skill) }, "preview")}
                                        actions={skill.source === "library" && (
                                            <IconButton label="Delete" icon={Trash} size="sm" onClick={() => setConfirmDelete(skill.name)} />
                                        )}
                                    />
                                ))}
                            </ul>
                        )}
                    </nav>

                    <section ref={detailRef} aria-label="Skill" className="min-w-0 scroll-mt-4">
                        {draft && (
                            <form
                                className="grid gap-5"
                                onSubmit={(e) => { e.preventDefault(); saveDraft(); }}
                            >
                                <DetailHead
                                    title={openKey === "new" ? "New skill" : <span className="font-code">{openSkill?.name}</span>}
                                    sub={openSkill && (
                                        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                                            {openSkill.source === "disk" ? (
                                                <Badge tone="neutral"><span className="inline-flex items-center gap-1"><HardDrive size={10} aria-hidden />Disk</span></Badge>
                                            ) : (
                                                <Badge tone="accent"><span className="inline-flex items-center gap-1"><Library size={10} aria-hidden />Library</span></Badge>
                                            )}
                                            {openSkill.shadowed && <Badge tone="warning">Shadowed by a library skill</Badge>}
                                            <span>{users.length ? `Used by ${users.join(", ")}` : "No agent uses it yet"}</span>
                                            {openSkill.path && (
                                                <span className="block w-full min-w-0 truncate font-code text-2xs text-text-faint" title={openSkill.path}>{openSkill.path}</span>
                                            )}
                                        </span>
                                    )}
                                    actions={
                                        <>
                                            {openSkill && draftTab === "preview" && (
                                                <IconButton label={editLabel(openSkill)} icon={Pencil} onClick={() => setDraftTab("edit")} />
                                            )}
                                            {openSkill?.source === "library" && (
                                                <IconButton label="Delete" icon={Trash} onClick={() => setConfirmDelete(openSkill.name)} />
                                            )}
                                            {(openKey === "new" || dirty) && (
                                                <Button type="button" variant="ghost" onClick={cancel}>Cancel</Button>
                                            )}
                                            {(openKey === "new" || dirty || draftTab === "edit") && (
                                                <Button type="submit" disabled={draftBusy}>
                                                    {draftBusy ? "Saving…" : "Save skill"}
                                                </Button>
                                            )}
                                        </>
                                    }
                                />
                                <p className="m-0 text-[13px] text-text-muted">
                                    Claude Code&apos;s SKILL.md shape: the description is always in the agent&apos;s prompt; the body loads only when the agent calls load_skill.
                                </p>
                                <Field
                                    label="Name"
                                    required
                                    hint={draft.existing ? "Names cannot change in place — agents select skills by name." : <>Lower-case, hyphens for spaces — e.g. <span className="font-code">quarterly-report-format</span></>}
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
                                <div className="space-y-1">
                                    <label htmlFor="skill-body" className="block text-sm font-medium text-text">Instructions (markdown)</label>
                                    {/* No `{{` suggestions: nothing in a skill body is filled in. The tab
                                        is controlled because an existing skill opens on Preview. */}
                                    <MarkdownEditor
                                        id="skill-body"
                                        tab={draftTab === "preview" ? "preview" : "write"}
                                        onTabChange={(next) => setDraftTab(next === "preview" ? "preview" : "edit")}
                                        value={draft.body}
                                        onChange={(body) => setDraft({ ...draft, body })}
                                        textareaClassName="h-[clamp(240px,calc(100vh-460px),640px)] resize-none"
                                        placeholder={"# My skill\n\nStep-by-step instructions the agent follows once loaded…"}
                                        previewEmpty="No instructions yet."
                                    />
                                    <p className="text-2xs text-text-faint">
                                        Used as written. Loaded into the agent&apos;s context only when it calls load_skill.
                                    </p>
                                </div>
                            </form>
                        )}
                    </section>
                </div>
            )}

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
                        hint={<>From <span className="font-code">skills.sh</span> — e.g. <span className="font-code">anthropics/skills</span> or <span className="font-code">vercel-labs/agent-skills@vercel-react-best-practices</span></>}
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
                        <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg px-3 py-2 font-code text-2xs text-text-muted">
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
                        <span className="font-code">{confirmDelete}</span> will stop being offered to
                        agents. Agents that selected it keep the name and skip it at run time.
                    </>
                }
            />

            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </AppScreen>
    );
}
