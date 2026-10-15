"use client";

import React, { useState } from 'react';
import { LifeBuoy, ChevronDown, ChevronRight, ExternalLink, BookOpen, Cpu, GitBranch, Wrench, Server, Key, Database, Code } from 'lucide-react';
import Link from 'next/link';

import { LinkButton, Section } from '@/components/ui';
import { cn } from '@/lib/cn';
import { FORM } from './form';

/** A plain section on this page: always open, its head over a hairline (M2g). */
const S = { variant: 'plain', collapsible: false } as const;
/** A link in running text. */
const LINK = 'text-accent-ink hover:underline';
/** A docs link under an answer: small, with the external mark. */
const DOC_LINK = 'inline-flex items-center gap-1 text-xs text-accent-ink hover:underline';

export const SupportTab = () => {
    const [openFaq, setOpenFaq] = useState<number | null>(0);

    const toggleFaq = (index: number) => {
        setOpenFaq(openFaq === index ? null : index);
    };

    const docLinks = [
        { icon: BookOpen, label: "Getting started", description: "Installation & quick setup", href: "https://docs.synapseorch.com/getting-started/installation" },
        { icon: Cpu, label: "Agents", description: "Agent types & configuration", href: "https://docs.synapseorch.com/agents/overview" },
        { icon: GitBranch, label: "Workflows", description: "Multi-agent DAG workflows", href: "https://docs.synapseorch.com/orchestrations/overview" },
        { icon: Key, label: "LLM providers", description: "Connect cloud & local models", href: "https://docs.synapseorch.com/llm-providers/overview" },
        { icon: Server, label: "Apps", description: "Model Context Protocol setup", href: "https://docs.synapseorch.com/mcp/overview" },
        { icon: Wrench, label: "Custom tools", description: "Python & HTTP tool builder", href: "https://docs.synapseorch.com/custom-tools/overview" },
        { icon: Database, label: "Vault", description: "Persistent files & knowledge bases", href: "https://docs.synapseorch.com/vault" },
        { icon: Code, label: "API reference", description: "REST API endpoints", href: "https://docs.synapseorch.com/api/overview" },
    ];

    const steps = [
        { title: "Configure models", href: "/settings/models", text: "Add API keys for cloud providers or select a local model via Ollama." },
        { title: "Add tools & servers (optional)", href: "/apps", text: "Connect apps or build custom tools so your agents can interact with the outside world." },
        { title: "Build agents", href: "/agents", text: "Create specialized agents with specific system prompts, models, and tool capabilities." },
        { title: "Create workflows", href: "/workflows", text: "Wire your agents together in a deterministic DAG to execute complex tasks efficiently." },
    ];

    const faqs = [
        {
            question: "How do I configure my LLMs?",
            answer: (
                <div className="space-y-2">
                    <p>
                        Synapse supports local models (via Ollama) and cloud providers (OpenAI, Anthropic, Gemini, Groq, etc.).
                    </p>
                    <ul className="list-disc pl-5 space-y-1">
                        <li>Go to the <Link href="/settings/models" className={LINK}>Models tab</Link>.</li>
                        <li>Enter your API keys for your preferred providers.</li>
                        <li>Select a default model for your agents to use.</li>
                    </ul>
                    <a href="https://docs.synapseorch.com/llm-providers/overview" target="_blank" rel="noopener noreferrer" className={cn(DOC_LINK, 'mt-1')}>
                        LLM providers docs <ExternalLink className="h-3 w-3" aria-hidden />
                    </a>
                </div>
            )
        },
        {
            question: "How can I add custom capabilities to my agents?",
            answer: (
                <div className="space-y-3">
                    <p>There are two main ways to extend your agents&apos; capabilities:</p>
                    <div>
                        <strong className="block font-medium text-text mb-0.5">1. Tool Builder (custom Python / HTTP)</strong>
                        <p>Write a Python script or configure an HTTP request (like n8n) in the <Link href="/tools" className={LINK}>Tools</Link>.</p>
                    </div>
                    <div>
                        <strong className="block font-medium text-text mb-0.5">2. Apps</strong>
                        <p>Connect external tools via the Model Context Protocol in <Link href="/apps" className={LINK}>Apps</Link>. Provide the connection command/URL, and the server&apos;s tools will be auto-registered.</p>
                    </div>
                    <div className="flex gap-4">
                        <a href="https://docs.synapseorch.com/custom-tools/overview" target="_blank" rel="noopener noreferrer" className={DOC_LINK}>
                            Custom tools docs <ExternalLink className="h-3 w-3" aria-hidden />
                        </a>
                        <a href="https://docs.synapseorch.com/mcp/overview" target="_blank" rel="noopener noreferrer" className={DOC_LINK}>
                            Apps docs <ExternalLink className="h-3 w-3" aria-hidden />
                        </a>
                    </div>
                </div>
            )
        },
        {
            question: "What is a workflow and how do I build one?",
            answer: (
                <div className="space-y-2">
                    <p>A workflow is a set of steps (agents, tools, people, logic) the engine runs in order, as a graph.</p>
                    <ol className="list-decimal pl-5 space-y-1">
                        <li>First, define specialized agents in the <Link href="/agents" className={LINK}>Agents</Link>.</li>
                        <li>Then, go to <Link href="/workflows" className={LINK}>Workflows</Link>.</li>
                        <li>Connect your agents into a sequence or complex flow, defining how tasks and data move between them.</li>
                    </ol>
                    <div className="flex gap-4 mt-1">
                        <a href="https://docs.synapseorch.com/agents/overview" target="_blank" rel="noopener noreferrer" className={DOC_LINK}>
                            Agents docs <ExternalLink className="h-3 w-3" aria-hidden />
                        </a>
                        <a href="https://docs.synapseorch.com/orchestrations/overview" target="_blank" rel="noopener noreferrer" className={DOC_LINK}>
                            Workflows docs <ExternalLink className="h-3 w-3" aria-hidden />
                        </a>
                    </div>
                </div>
            )
        },
        {
            question: "What is the Vault used for?",
            answer: (
                <div className="space-y-2">
                    <p>
                        The <Link href="/vault" className={LINK}>Vault</Link> stores persistent files, knowledge bases, and skills.
                    </p>
                    <p className="rounded-md bg-surface-2 px-3 py-2">
                        <strong className="font-medium text-text">Pro tip:</strong> Agents can reference vault files directly in their prompts using the <code className="font-code text-text">@[path]</code> syntax. This gives them immediate access to essential context.
                    </p>
                    <a href="https://docs.synapseorch.com/vault" target="_blank" rel="noopener noreferrer" className={DOC_LINK}>
                        Vault docs <ExternalLink className="h-3 w-3" aria-hidden />
                    </a>
                </div>
            )
        },
        {
            question: "Where can I view system logs?",
            answer: (
                <div className="space-y-2">
                    <p>
                        Monitor system activity, agent executions, and tool calls in <Link href="/logs" className={LINK}>Runs › Logs</Link>.
                    </p>
                    <p>
                        This is crucial for debugging complex workflows, tracking token usage, and ensuring everything is running smoothly.
                    </p>
                </div>
            )
        }
    ];

    return (
        <div>

            {/* Documentation */}
            <Section {...S} title="Documentation"
                actions={
                    <a
                        href="https://docs.synapseorch.com"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-xs text-accent-ink hover:underline"
                    >
                        docs.synapseorch.com <ExternalLink className="h-3 w-3" aria-hidden />
                    </a>
                }
            >
                <ul className={`${FORM} grid gap-x-8 sm:grid-cols-2`}>
                    {docLinks.map(({ icon: Icon, label, description, href }) => (
                        <li key={href} className="border-b border-border">
                            <a
                                href={href}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="group -mx-2 flex items-start gap-3 rounded-md px-2 py-2.5 transition-colors hover:bg-row-hover"
                            >
                                <Icon className="mt-0.5 size-4 shrink-0 text-text-faint" aria-hidden />
                                <span className="min-w-0 flex-1">
                                    <span className="block text-sm font-medium text-text group-hover:text-accent-ink">{label}</span>
                                    <span className="block truncate text-xs text-text-faint">{description}</span>
                                </span>
                                <ExternalLink className="mt-1 size-3 shrink-0 text-text-faint group-hover:text-accent-ink" aria-hidden />
                            </a>
                        </li>
                    ))}
                </ul>
            </Section>

            {/* Community */}
            <Section {...S} title="Community">
                <div className={`${FORM} flex flex-col gap-4 md:flex-row md:items-center md:justify-between`}>
                    <div>
                        <h3 className="mb-1 text-sm font-medium text-text">Join the community</h3>
                        <p className="max-w-xl text-sm text-text-muted">
                            Have questions, need help debugging, or want to share your custom workflows? Join our active Discord community to connect with other builders.
                        </p>
                    </div>
                    <LinkButton
                        external
                        href="https://discord.gg/9UN45qyGh8"
                        target="_blank"
                        rel="noopener noreferrer"
                        variant="secondary"
                        size="sm"
                    >
                        <LifeBuoy className="size-3.5 text-text-faint" aria-hidden />
                        Join Discord server
                        <ExternalLink className="size-3 text-text-faint" aria-hidden />
                    </LinkButton>
                </div>
            </Section>

            {/* Quick start guide */}
            <Section {...S} title="Quick start guide"
                description="Follow these steps to build your first multi-agent workflow."
                actions={
                    <a
                        href="https://docs.synapseorch.com/getting-started/installation"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-xs text-accent-ink hover:underline"
                    >
                        Full guide <ExternalLink className="h-3 w-3" aria-hidden />
                    </a>
                }
            >
                <ol className={FORM}>
                    {steps.map((step, i) => (
                        <li key={step.href} className="flex items-start gap-4 border-b border-border py-3 last:border-b-0">
                            <span className="flex size-6 shrink-0 items-center justify-center rounded-full border border-border-strong text-xs font-medium tabular-nums text-text-muted">
                                {i + 1}
                            </span>
                            <div className="min-w-0 flex-1">
                                <h3 className="text-sm font-medium text-text">{step.title}</h3>
                                <p className="mt-0.5 text-xs text-text-muted">{step.text}</p>
                            </div>
                            <Link href={step.href} className="mt-0.5 inline-flex shrink-0 items-center gap-0.5 rounded-sm text-xs font-medium text-accent-ink hover:underline">
                                Go <ChevronRight className="size-3" aria-hidden />
                            </Link>
                        </li>
                    ))}
                </ol>
            </Section>

            {/* Frequently asked questions */}
            <Section {...S} title="Frequently asked questions">
                <div className={`${FORM} border-y border-border divide-y divide-border`}>
                    {faqs.map((faq, index) => (
                        <div key={index}>
                            <button
                                onClick={() => toggleFaq(index)}
                                aria-expanded={openFaq === index}
                                className="group flex w-full items-center justify-between rounded-md py-3 text-left"
                            >
                                <span className="pr-4 text-sm font-medium text-text transition-colors group-hover:text-accent-ink">{faq.question}</span>
                                <ChevronDown
                                    className={`h-4 w-4 shrink-0 text-text-faint transition-transform duration-300 ${openFaq === index ? 'rotate-180' : ''}`}
                                    aria-hidden
                                />
                            </button>
                            <div
                                className={`overflow-hidden transition-all duration-300 ease-in-out ${openFaq === index ? 'max-h-[500px] opacity-100' : 'max-h-0 opacity-0'}`}
                            >
                                <div className="pb-4 text-sm leading-relaxed text-text-muted">
                                    {faq.answer}
                                </div>
                            </div>
                        </div>
                    ))}
                </div>
            </Section>

        </div>
    );
};
