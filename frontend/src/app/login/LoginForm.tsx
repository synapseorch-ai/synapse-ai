"use client";

import { useState } from 'react';
import { useRouter } from 'next/navigation';

export function LoginForm({ redirectTo }: { redirectTo: string }) {
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

    return (
        <div className="min-h-screen bg-black flex items-center justify-center px-4">
            <div className="w-full max-w-sm space-y-8">
                <div>
                    <h1 className="text-2xl font-bold text-white tracking-tight">Synapse AI</h1>
                    <p className="text-text-faint text-sm mt-1">Sign in to continue</p>
                </div>

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
