'use client';

/*
 * Home's ask line (ASSIST1, D291): a question for Synapse AI, typed or one of a
 * few suggestions that follow the workspace (`askSuggestions`). Asked at once:
 * a question reads the workspace and changes nothing, and anything it would do
 * comes back as a card to press. It sits above Needs you, which stays first in
 * what Home shows.
 */
import { useState } from 'react';

import { Button, Mark } from '@/components/ui';

export function AskLine({ suggestions, onAsk }: { suggestions: string[]; onAsk: (text: string) => void }) {
    const [text, setText] = useState('');
    const chip = 'inline-flex h-8 items-center rounded-full border border-border px-3 text-[13px] text-text-muted transition-colors hover:border-border-strong hover:text-text';
    return (
        <form
            className="grid min-w-0 gap-2.5"
            aria-label="Ask Synapse AI"
            onSubmit={(e) => {
                e.preventDefault();
                if (!text.trim()) return;
                onAsk(text.trim());
                setText('');
            }}
        >
            <label htmlFor="home-ask" className="sr-only">Ask Synapse AI about your workspace</label>
            <div className="flex items-center gap-2 rounded-xl border border-border-strong bg-surface py-1.5 pl-3 pr-1.5 transition-colors focus-within:border-accent">
                <Mark size={20} compact={false} className="text-accent" />
                <input
                    id="home-ask"
                    data-no-focus-ring
                    // No intrinsic width: on a phone the field shrinks to what is left beside the button.
                    size={1}
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    placeholder="Ask Synapse AI about your workspace, or what to build"
                    className="min-w-0 flex-1 bg-transparent py-1 text-sm text-text outline-none placeholder:text-text-faint"
                />
                <Button size="sm" type="submit" disabled={!text.trim()}>Ask</Button>
            </div>
            {suggestions.length > 0 && (
                <div className="flex flex-wrap gap-2" aria-label="Things to ask">
                    {suggestions.map((s) => (
                        <button key={s} type="button" className={chip} onClick={() => onAsk(s)}>{s}</button>
                    ))}
                </div>
            )}
        </form>
    );
}
