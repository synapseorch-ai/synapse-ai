"use client";
import { Settings, X, Shield, Trash, Cpu, Cloud, Database, LayoutGrid, Bot, Wrench, Server, FolderGit2, Workflow, ScrollText, MessageSquare, DollarSign, Clock, ArrowLeftRight, Vault, LifeBuoy, Key } from 'lucide-react';
import { useRouter, usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';

const tabs = [
    { id: 'general', label: 'General', icon: LayoutGrid },
    { id: 'personal_details', label: 'Personal Details', icon: Shield },
    { id: 'orchestrations', label: 'Orchestrations', icon: Workflow },
    { id: 'agents', label: 'Build Agents', icon: Bot },
    { id: 'mcp_servers', label: 'MCP Servers', icon: Server },
    { id: 'custom_tools', label: 'Tool Builder', icon: Wrench },
    { id: 'repos', label: 'Repos', icon: FolderGit2 },
    { id: 'db_configs', label: 'DB Configs', icon: Database },
    { id: 'models', label: 'Models', icon: Cpu },
    { id: 'messaging', label: 'Messaging', icon: MessageSquare },
    { id: 'workspace', label: 'Integrations', icon: Cloud },
    { id: 'schedules', label: 'Schedules', icon: Clock },
    { id: 'vault', label: 'Vault', icon: Vault },
    { id: 'usage', label: 'Usage', icon: DollarSign },
    { id: 'import_export', label: 'Import / Export', icon: ArrowLeftRight },
    { id: 'logs', label: 'Logs', icon: ScrollText },
    { id: 'memory', label: 'Memory', icon: Trash },
    { id: 'api_keys', label: 'API Keys', icon: Key },
    { id: 'support', label: 'Support & Docs', icon: LifeBuoy },
];

export default function SettingsLayout({
    children,
}: {
    children: React.ReactNode;
}) {
    const router = useRouter();
    const pathname = usePathname();
    const activeTabMatch = pathname.match(/\/settings\/([^\/]+)/);
    const activeTab = activeTabMatch ? activeTabMatch[1] : 'general';

    const [messagingEnabled, setMessagingEnabled] = useState(false);
    const [codingEnabled, setCodingEnabled] = useState(false);
    const [theme, setTheme] = useState<'dark' | 'light'>('dark');

    // Read persisted theme AFTER hydration to avoid SSR mismatch
    useEffect(() => {
        const saved = localStorage.getItem('synapseTheme') as 'dark' | 'light' | null;
        if (saved) setTheme(saved);
    }, []);

    useEffect(() => {
        // Quick fetch just to conditionally render tabs, or we can just render all tabs locally
        fetch('/api/settings').then(r => r.json()).then(data => {
            setMessagingEnabled(data.messaging_enabled || false);
            setCodingEnabled(data.coding_agent_enabled || false);
        }).catch(() => { });
        // Close on escape
        const handleEsc = (e: KeyboardEvent) => {
            if (e.key === 'Escape') router.push('/');
        };
        document.addEventListener('keydown', handleEsc);
        return () => document.removeEventListener('keydown', handleEsc);
    }, [router]);

    const displayTabs = tabs.filter(t => {
        if (t.id === 'messaging' && !messagingEnabled) return false;
        if ((t.id === 'repos' || t.id === 'db_configs') && !codingEnabled) return false;
        return true;
    });

    return (
        <div className={`fixed inset-0 z-50 flex items-center justify-center bg-black/90 backdrop-blur-md animate-in fade-in duration-200 font-mono${theme === 'light' ? ' light-mode' : ''}`}>
            <div className="w-full h-full bg-black shadow-2xl flex flex-col md:flex-row overflow-hidden relative">

                {/* Header (Mobile) / Close Button */}
                <button
                    onClick={() => router.push('/')}
                    className="absolute top-4 right-4 z-50 p-2 text-zinc-500 hover:text-white hover:bg-zinc-900 transition-colors"
                >
                    <X className="h-6 w-6" />
                </button>

                {/* Sidebar */}
                <div className="w-full md:w-64 bg-zinc-950 border-b md:border-b-0 md:border-r border-zinc-800 flex flex-col shrink-0">
                    <div className="p-6 border-b border-zinc-800 md:mb-2">
                        <h2 className="text-xl font-bold flex items-center gap-3 tracking-wider text-zinc-50">
                            <Settings className="h-5 w-5" />
                            SETTINGS
                        </h2>
                    </div>

                    <nav className="flex-1 p-2 space-y-1 overflow-x-auto overflow-y-hidden md:overflow-x-hidden md:overflow-y-auto flex md:flex-col modern-scrollbar">
                        {displayTabs.map((tab) => {
                            const Icon = tab.icon;
                            const isActive = activeTab === tab.id;
                            return (
                                <button
                                    key={tab.id}
                                    onClick={() => router.push(`/settings/${tab.id}`)}
                                    className={`flex items-center gap-3 px-4 py-2.5 text-sm font-medium transition-all duration-200 whitespace-nowrap md:whitespace-normal
                                        ${isActive
                                            ? 'bg-zinc-50 text-zinc-950 shadow-lg'
                                            : 'text-zinc-400 hover:text-zinc-50 hover:bg-zinc-800'
                                        }`}
                                >
                                    <Icon className={`h-4 w-4 ${isActive ? 'text-zinc-950' : 'text-zinc-500'}`} />
                                    {tab.label}
                                </button>
                            );
                        })}
                    </nav>

                    <div className="p-4 border-t border-zinc-800 hidden md:block">
                        <div className="text-[10px] text-zinc-500 font-mono text-center">
                            Synapse v1.0
                        </div>
                    </div>
                </div>

                {/* Main Content Area */}
                <div className="flex-1 flex flex-col h-full overflow-hidden bg-transparent relative">
                    {children}
                </div>

            </div>
        </div>
    );
}
