"use client";

import { useState } from 'react';
import { useRouter } from 'next/navigation';

export function LoginForm({
    redirectTo,
    mode = 'password',
    linkProblem,
}: {
    redirectTo: string;
    /** `access`: login is off and this install is reached with its access link (D124). */
    mode?: 'password' | 'access';
    /** Set when a link did not work: `invalid`, or `login-on` (a password wins). */
    linkProblem?: string;
}) {
    const router = useRouter();

    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [isLoading, setIsLoading] = useState(false);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setIsLoading(true);

        try {
            const res = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password }),
            });

            if (res.ok) {
                router.replace(redirectTo);
                // Refresh so the RSC router cache re-fetches with the freshly-set
                // session cookie present (guards against router-cache staleness).
                router.refresh();
            } else {
                const data = await res.json().catch(() => ({}));
                setError(data.error || 'Invalid username or password');
            }
        } catch {
            setError('Connection error. Please try again.');
        } finally {
            setIsLoading(false);
        }
    };

    if (mode === 'access') {
        return <AccessLinkForm redirectTo={redirectTo} linkProblem={linkProblem} />;
    }

    return (
        <div className="min-h-screen bg-black flex items-center justify-center px-4">
            <div className="w-full max-w-sm space-y-8">
                <div>
                    <h1 className="text-2xl font-bold text-white tracking-tight">Synapse AI</h1>
                    <p className="text-text-faint text-sm mt-1">Sign in to continue</p>
                </div>

                {linkProblem === 'login-on' && (
                    <p className="text-xs text-text-faint">
                        Login is on for this Synapse, so its access link no longer opens it. Sign in instead.
                    </p>
                )}

                <form onSubmit={handleSubmit} className="space-y-4">
                    <div className="space-y-1.5">
                        <label htmlFor="login-username" className="text-[11px] uppercase font-bold text-text-faint tracking-widest">
                            Username
                        </label>
                        <input
                            id="login-username"
                            type="text"
                            value={username}
                            onChange={e => setUsername(e.target.value)}
                            required
                            autoFocus
                            autoComplete="username"
                            className="w-full bg-zinc-900 border border-zinc-800 px-3 py-2.5 text-sm
                                       focus:border-zinc-500 focus:outline-none transition-colors
                                       text-white placeholder:text-zinc-700 rounded-md"
                            placeholder="Enter username"
                        />
                    </div>

                    <div className="space-y-1.5">
                        <label htmlFor="login-password" className="text-[11px] uppercase font-bold text-text-faint tracking-widest">
                            Password
                        </label>
                        <input
                            id="login-password"
                            type="password"
                            value={password}
                            onChange={e => setPassword(e.target.value)}
                            required
                            autoComplete="current-password"
                            className="w-full bg-zinc-900 border border-zinc-800 px-3 py-2.5 text-sm
                                       focus:border-zinc-500 focus:outline-none transition-colors
                                       text-white placeholder:text-zinc-700 rounded-md"
                            placeholder="Enter password"
                        />
                    </div>

                    {error && (
                        <p className="text-red-400 text-xs">{error}</p>
                    )}

                    <button
                        type="submit"
                        disabled={isLoading}
                        className="w-full px-6 py-2.5 text-sm font-bold bg-accent text-accent-fg
                                   hover:bg-accent-hover transition-all disabled:opacity-50
                                   disabled:cursor-not-allowed mt-2"
                    >
                        {isLoading ? 'Signing in…' : 'Sign In'}
                    </button>
                </form>

                <p className="text-xs text-text-faint text-center">
                    Forgot your password?{' '}
                    Run{' '}
                    <code className="text-zinc-400 bg-zinc-900 px-1.5 py-0.5 font-code text-[11px]">
                        synapse reset-password
                    </code>
                    {' '}in your terminal.
                </p>
            </div>
        </div>
    );
}

/**
 * Login is off, and this install is reachable only with its access link.
 *
 * Opening the link signs you in without this screen; someone who arrives
 * without it lands here, told where the link is and able to paste it.
 */
function AccessLinkForm({ redirectTo, linkProblem }: { redirectTo: string; linkProblem?: string }) {
    const router = useRouter();
    const [link, setLink] = useState('');
    const [error, setError] = useState(linkProblem === 'invalid' ? 'That link is not this Synapse\'s access link.' : '');
    const [isLoading, setIsLoading] = useState(false);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setIsLoading(true);
        try {
            const res = await fetch('/api/auth/access', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: link }),
            });
            if (res.ok) {
                router.replace(redirectTo);
                router.refresh();
            } else {
                const data = await res.json().catch(() => ({}));
                setError(data.error || 'That link did not work.');
            }
        } catch {
            setError('Connection error. Please try again.');
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div className="min-h-screen bg-black flex items-center justify-center px-4">
            <div className="w-full max-w-sm space-y-8">
                <div>
                    <h1 className="text-2xl font-bold text-white tracking-tight">Synapse AI</h1>
                    <p className="text-text-faint text-sm mt-1">This Synapse needs its access link</p>
                </div>

                <p className="text-sm text-text-muted">
                    Login is off, so Synapse is opened with the link it printed when it started. Run{' '}
                    <code className="text-zinc-400 bg-zinc-900 px-1.5 py-0.5 font-code text-[11px]">synapse access-link</code>{' '}
                    on the machine running it, or find the link in its logs, then open it or paste it here.
                </p>

                <form onSubmit={handleSubmit} className="space-y-4">
                    <div className="space-y-1.5">
                        <label htmlFor="access-link" className="text-[11px] uppercase font-bold text-text-faint tracking-widest">
                            Access link
                        </label>
                        <input
                            id="access-link"
                            type="password"
                            value={link}
                            onChange={e => setLink(e.target.value)}
                            required
                            autoFocus
                            autoComplete="off"
                            spellCheck={false}
                            className="w-full bg-zinc-900 border border-zinc-800 px-3 py-2.5 text-sm
                                       focus:border-zinc-500 focus:outline-none transition-colors
                                       text-white placeholder:text-zinc-700 rounded-md"
                            placeholder="http://…/?token=…"
                        />
                    </div>

                    {error && (
                        <p role="alert" className="text-red-400 text-xs">{error}</p>
                    )}

                    <button
                        type="submit"
                        disabled={isLoading}
                        className="w-full px-6 py-2.5 text-sm font-bold bg-accent text-accent-fg
                                   hover:bg-accent-hover transition-all disabled:opacity-50
                                   disabled:cursor-not-allowed mt-2"
                    >
                        {isLoading ? 'Opening…' : 'Open Synapse'}
                    </button>
                </form>

                <p className="text-xs text-text-faint text-center">
                    Want a password instead? Turn on login in Settings → General.
                </p>
            </div>
        </div>
    );
}
