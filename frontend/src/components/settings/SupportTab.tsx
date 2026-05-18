"use client";

import React, { useState } from 'react';
import { LifeBuoy, ChevronDown, ChevronRight, ExternalLink, BookOpen, Cpu, GitBranch, Wrench, Server, Key, Database, Code } from 'lucide-react';
import Link from 'next/link';

export const SupportTab = () => {
    const [openFaq, setOpenFaq] = useState<number | null>(0);

    const toggleFaq = (index: number) => {
        setOpenFaq(openFaq === index ? null : index);
    };

    const docLinks = [
        { icon: BookOpen, label: "Getting Started", description: "Installation & quick setup", href: "https://docs.synapseorch.com/getting-started/installation" },
        { icon: Cpu, label: "Agents", description: "Agent types & configuration", href: "https://docs.synapseorch.com/agents/overview" },
        { icon: GitBranch, label: "Orchestrations", description: "Multi-agent DAG workflows", href: "https://docs.synapseorch.com/orchestrations/overview" },
        { icon: Key, label: "LLM Providers", description: "Connect cloud & local models", href: "https://docs.synapseorch.com/llm-providers/overview" },
        { icon: Server, label: "MCP Servers", description: "Model Context Protocol setup", href: "https://docs.synapseorch.com/mcp/overview" },
        { icon: Wrench, label: "Custom Tools", description: "Python & HTTP tool builder", href: "https://docs.synapseorch.com/custom-tools/overview" },
        { icon: Database, label: "Vault", description: "Persistent files & knowledge bases", href: "https://docs.synapseorch.com/vault" },
        { icon: Code, label: "API Reference", description: "REST API endpoints", href: "https://docs.synapseorch.com/api/overview" },
    ];

    const faqs = [
        {
            question: "How do I configure my LLMs?",
            answer: (
                <div className="space-y-2 text-zinc-400">
                    <p>
                        Synapse supports local models (via Ollama) and cloud providers (OpenAI, Anthropic, Gemini, Groq, etc.).
                    </p>
                    <ul className="list-disc pl-5 space-y-1 text-sm">
                        <li>Go to the <Link href="/settings/models" className="text-blue-400 hover:underline">Models tab</Link>.</li>
                        <li>Enter your API keys for your preferred providers.</li>
                        <li>Select a default model for your agents to use.</li>
                    </ul>
                    <a href="https://docs.synapseorch.com/llm-providers/overview" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-blue-400 hover:underline mt-1">
                        LLM Providers docs <ExternalLink className="h-3 w-3" />
                    </a>
                </div>
            )
        },
        {
            question: "How can I add custom capabilities to my agents?",
            answer: (
                <div className="space-y-3 text-zinc-400">
                    <p>There are two main ways to extend your agents' capabilities:</p>
                    <div className="bg-zinc-950 p-3 border border-zinc-800">
                        <strong className="text-zinc-300 block mb-1">1. Tool Builder (Custom Python / HTTP)</strong>
                        <p className="text-sm">Write a Python script or configure an HTTP request (like n8n) in the <Link href="/settings/custom_tools" className="text-blue-400 hover:underline">Tool Builder tab</Link>.</p>
                    </div>
                    <div className="bg-zinc-950 p-3 border border-zinc-800">
                        <strong className="text-zinc-300 block mb-1">2. MCP Servers</strong>
                        <p className="text-sm">Connect external tools via the Model Context Protocol in the <Link href="/settings/mcp_servers" className="text-blue-400 hover:underline">MCP Servers tab</Link>. Provide the connection command/URL, and the server's tools will be auto-registered.</p>
                    </div>
                    <div className="flex gap-3">
                        <a href="https://docs.synapseorch.com/custom-tools/overview" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-blue-400 hover:underline">
                            Custom Tools docs <ExternalLink className="h-3 w-3" />
                        </a>
                        <a href="https://docs.synapseorch.com/mcp/overview" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-blue-400 hover:underline">
                            MCP Servers docs <ExternalLink className="h-3 w-3" />
                        </a>
                    </div>
                </div>
            )
        },
        {
            question: "What is an Orchestration and how do I build one?",
            answer: (
                <div className="space-y-2 text-zinc-400">
                    <p>An Orchestration is a multi-agent workflow defined as a Directed Acyclic Graph (DAG).</p>
                    <ol className="list-decimal pl-5 space-y-1 text-sm">
                        <li>First, define specialized agents in the <Link href="/settings/agents" className="text-blue-400 hover:underline">Build Agents tab</Link>.</li>
                        <li>Then, go to the <Link href="/settings/orchestrations" className="text-blue-400 hover:underline">Orchestrations tab</Link>.</li>
                        <li>Connect your agents into a sequence or complex flow, defining how tasks and data move between them.</li>
                    </ol>
                    <div className="flex gap-3 mt-1">
                        <a href="https://docs.synapseorch.com/agents/overview" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-blue-400 hover:underline">
                            Agents docs <ExternalLink className="h-3 w-3" />
                        </a>
                        <a href="https://docs.synapseorch.com/orchestrations/overview" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-blue-400 hover:underline">
                            Orchestrations docs <ExternalLink className="h-3 w-3" />
                        </a>
                    </div>
                </div>
            )
        },
        {
            question: "What is the Vault used for?",
            answer: (
                <div className="space-y-2 text-zinc-400">
                    <p>
                        The <Link href="/settings/vault" className="text-blue-400 hover:underline">Vault</Link> stores persistent files, knowledge bases, and skills.
                    </p>
                    <p className="text-sm bg-zinc-950 p-2 border border-zinc-800">
                        <strong>Pro Tip:</strong> Agents can reference vault files directly in their prompts using the <code className="text-zinc-300 bg-zinc-900 px-1 py-0.5">@[path]</code> syntax. This gives them immediate access to essential context.
                    </p>
                    <a href="https://docs.synapseorch.com/vault" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-blue-400 hover:underline">
                        Vault docs <ExternalLink className="h-3 w-3" />
                    </a>
                </div>
            )
        },
        {
            question: "Where can I view system logs?",
            answer: (
                <div className="space-y-2 text-zinc-400 text-sm">
                    <p>
                        Monitor system activity, agent executions, and tool calls in the <Link href="/settings/logs" className="text-blue-400 hover:underline">Logs tab</Link>.
                    </p>
                    <p>
                        This is crucial for debugging complex orchestrations, tracking token usage, and ensuring everything is running smoothly.
                    </p>
                </div>
            )
        }
    ];

    return (
        <div className="space-y-8 text-zinc-300">

            {/* Documentation */}
            <div className="space-y-4">
                <div className="flex items-center justify-between">
                    <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Documentation</label>
                    <a
                        href="https://docs.synapseorch.com"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:underline"
                    >
                        docs.synapseorch.com <ExternalLink className="h-3 w-3" />
                    </a>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    {docLinks.map(({ icon: Icon, label, description, href }) => (
                        <a
                            key={href}
                            href={href}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="group bg-zinc-900 border border-zinc-800 p-3 hover:border-zinc-600 hover:bg-zinc-800/50 transition-colors"
                        >
                            <Icon className="h-4 w-4 text-zinc-500 group-hover:text-zinc-300 mb-2 transition-colors" />
                            <div className="text-xs font-semibold text-zinc-300 group-hover:text-white transition-colors">{label}</div>
                            <div className="text-[10px] text-zinc-600 mt-0.5">{description}</div>
                        </a>
                    ))}
                </div>
            </div>

            {/* Discord Callout */}
            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Community</label>
                <div className="bg-zinc-900 border border-zinc-800 p-4">
                    <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                        <div>
                            <h3 className="text-sm font-bold text-zinc-200 flex items-center gap-2 mb-1">
                                <LifeBuoy className="h-4 w-4 text-[#5865F2]" />
                                Join the Community
                            </h3>
                            <p className="text-xs text-zinc-600 max-w-xl">
                                Have questions, need help debugging, or want to share your custom orchestrations? Join our active Discord community to connect with other builders.
                            </p>
                        </div>
                        <a
                            href="https://discord.gg/9UN45qyGh8"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex shrink-0 items-center gap-2 px-4 py-2 bg-[#5865F2] hover:bg-[#4752C4] text-white text-xs font-bold transition-colors"
                        >
                            Join Discord Server
                            <ExternalLink className="h-3.5 w-3.5" />
                        </a>
                    </div>
                </div>
            </div>

            {/* Quick Start Guide */}
            <div className="space-y-4">
                <div className="flex items-center justify-between">
                    <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Quick Start Guide</label>
                    <a
                        href="https://docs.synapseorch.com/getting-started/installation"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:underline"
                    >
                        Full guide <ExternalLink className="h-3 w-3" />
                    </a>
                </div>
                <p className="text-xs text-zinc-600">
                    Follow these steps to build your first multi-agent workflow.
                </p>

                <div className="space-y-3">

                    <div className="flex gap-4 group">
                        <div className="flex-shrink-0 flex items-center justify-center w-7 h-7 bg-zinc-900 border border-zinc-800 text-zinc-400 font-bold text-xs group-hover:bg-white group-hover:text-black group-hover:border-white transition-colors">
                            1
                        </div>
                        <div className="flex-1">
                            <h3 className="text-sm font-semibold text-zinc-200 mb-0.5 flex items-center gap-2">
                                Configure Models
                                <Link href="/settings/models" className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 bg-zinc-900 border border-zinc-800 hover:bg-zinc-800 text-zinc-500 hover:text-zinc-300 transition-colors flex items-center gap-1">
                                    Go <ChevronRight className="h-2.5 w-2.5" />
                                </Link>
                            </h3>
                            <p className="text-xs text-zinc-600">Add API keys for cloud providers or select a local model via Ollama.</p>
                        </div>
                    </div>

                    <div className="flex gap-4 group">
                        <div className="flex-shrink-0 flex items-center justify-center w-7 h-7 bg-zinc-900 border border-zinc-800 text-zinc-400 font-bold text-xs group-hover:bg-white group-hover:text-black group-hover:border-white transition-colors">
                            2
                        </div>
                        <div className="flex-1">
                            <h3 className="text-sm font-semibold text-zinc-200 mb-0.5 flex items-center gap-2">
                                Add Tools & Servers (Optional)
                                <Link href="/settings/mcp_servers" className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 bg-zinc-900 border border-zinc-800 hover:bg-zinc-800 text-zinc-500 hover:text-zinc-300 transition-colors flex items-center gap-1">
                                    Go <ChevronRight className="h-2.5 w-2.5" />
                                </Link>
                            </h3>
                            <p className="text-xs text-zinc-600">Connect MCP servers or build custom tools so your agents can interact with the outside world.</p>
                        </div>
                    </div>

                    <div className="flex gap-4 group">
                        <div className="flex-shrink-0 flex items-center justify-center w-7 h-7 bg-zinc-900 border border-zinc-800 text-zinc-400 font-bold text-xs group-hover:bg-white group-hover:text-black group-hover:border-white transition-colors">
                            3
                        </div>
                        <div className="flex-1">
                            <h3 className="text-sm font-semibold text-zinc-200 mb-0.5 flex items-center gap-2">
                                Build Agents
                                <Link href="/settings/agents" className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 bg-zinc-900 border border-zinc-800 hover:bg-zinc-800 text-zinc-500 hover:text-zinc-300 transition-colors flex items-center gap-1">
                                    Go <ChevronRight className="h-2.5 w-2.5" />
                                </Link>
                            </h3>
                            <p className="text-xs text-zinc-600">Create specialized agents with specific system prompts, models, and tool capabilities.</p>
                        </div>
                    </div>

                    <div className="flex gap-4 group">
                        <div className="flex-shrink-0 flex items-center justify-center w-7 h-7 bg-zinc-900 border border-zinc-800 text-zinc-400 font-bold text-xs group-hover:bg-white group-hover:text-black group-hover:border-white transition-colors">
                            4
                        </div>
                        <div className="flex-1">
                            <h3 className="text-sm font-semibold text-zinc-200 mb-0.5 flex items-center gap-2">
                                Create Orchestrations
                                <Link href="/settings/orchestrations" className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 bg-zinc-900 border border-zinc-800 hover:bg-zinc-800 text-zinc-500 hover:text-zinc-300 transition-colors flex items-center gap-1">
                                    Go <ChevronRight className="h-2.5 w-2.5" />
                                </Link>
                            </h3>
                            <p className="text-xs text-zinc-600">Wire your agents together in a deterministic DAG to execute complex tasks efficiently.</p>
                        </div>
                    </div>

                </div>
            </div>

            {/* FAQ Accordion */}
            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Frequently Asked Questions</label>
                <div className="border border-zinc-800 divide-y divide-zinc-800">
                    {faqs.map((faq, index) => (
                        <div key={index} className="bg-zinc-900/30">
                            <button
                                onClick={() => toggleFaq(index)}
                                className="w-full text-left px-4 py-3 flex items-center justify-between hover:bg-zinc-800/30 transition-colors focus:outline-none"
                            >
                                <span className="font-medium text-sm text-zinc-200 pr-4">{faq.question}</span>
                                <ChevronDown
                                    className={`h-4 w-4 text-zinc-500 shrink-0 transition-transform duration-300 ${openFaq === index ? 'rotate-180 text-zinc-300' : ''}`}
                                />
                            </button>
                            <div
                                className={`overflow-hidden transition-all duration-300 ease-in-out ${openFaq === index ? 'max-h-[500px] opacity-100' : 'max-h-0 opacity-0'}`}
                            >
                                <div className="px-4 pb-4 pt-1">
                                    {faq.answer}
                                </div>
                            </div>
                        </div>
                    ))}
                </div>
            </div>

        </div>
    );
};
