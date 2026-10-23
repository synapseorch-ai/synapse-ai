'use client';

/*
 * The panel the "?" opens: the kit `Drawer`, so focus moves in, Tab stays inside,
 * Escape, the ✕ and the scrim close it, and focus goes back to the "?" (F382).
 *
 * The words come from the product's own topic and need nothing from the network,
 * so they are all there offline. The picture is the docs site's, and it is only
 * ever an extra: offline it is not drawn at all (no request, no broken image),
 * and it is drawn only once its still has loaded, so one that fails, or is not
 * published yet, never shows a frame at all (F900). Under reduced motion a clip is shown as its still, and the clip
 * itself is never fetched.
 */
import { Pause, Play } from 'lucide-react';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { Drawer, TextLink, buttonStyles } from '@/components/ui';
import { cn } from '@/lib/cn';

import { useHelp, type HelpMedia } from './host';

export function HelpPanel() {
    const help = useHelp();
    if (!help?.topic) return null;
    const { host, open, setOpen, topic } = help;
    return (
        <Drawer
            open={open}
            onClose={() => setOpen(false)}
            title={topic.title}
            description={topic.summary}
            footer={
                <TextLink
                    external
                    href={`${host.docsBase}${topic.guide}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm font-medium"
                >
                    Read the guide <span aria-hidden>→</span>
                    <span className="sr-only"> (opens in a new tab)</span>
                </TextLink>
            }
        >
            <div className="space-y-5">
                {topic.media && <HelpPicture key={topic.media.id} media={topic.media} base={host.mediaBase} />}
                <ol className="list-decimal space-y-2.5 pl-5 text-sm leading-relaxed text-text marker:text-text-muted">
                    {topic.steps.map(step => (
                        <li key={step} className="pl-1">{step}</li>
                    ))}
                </ol>
            </div>
        </Drawer>
    );
}

/** HC0's stills are 1440×900 and its clips 1280×800: one 16:10 frame holds either without moving the text. */
function HelpPicture({ media, base }: { media: HelpMedia; base: string }) {
    const online = useOnline();
    const reduced = useReducedMotion();
    const [failed, setFailed] = useState<'none' | 'clip' | 'all'>('none');
    const [loaded, setLoaded] = useState(false);
    const [paused, setPaused] = useState(false);
    const video = useRef<HTMLVideoElement>(null);
    const still = `${base}/${media.id}.webp`;
    // The frame is drawn only once its still has loaded (F900): drawn first, a picture the docs site does not have
    // yet showed an empty frame for a moment and then took it away, moving the words twice. The still is the clip's
    // poster too, so a clip waits for it as well.
    const [ready, setReady] = useState(false);
    useEffect(() => {
        if (!online) return;
        const probe = new Image();
        probe.onload = () => setReady(true);
        probe.onerror = () => setFailed('all');
        probe.src = still;
        return () => { probe.onload = null; probe.onerror = null; };
    }, [still, online]);

    if (!online || failed === 'all' || !ready) return null;

    const frame = 'relative aspect-[16/10] w-full overflow-hidden rounded-md border border-border bg-surface-2';

    if (media.clip && !reduced && failed !== 'clip') {
        return (
            <figure className={frame}>
                <video
                    ref={video}
                    src={`${base}/${media.id}.webm`}
                    poster={still}
                    width={1280}
                    height={800}
                    autoPlay
                    muted
                    loop
                    playsInline
                    aria-label={media.alt}
                    onPlay={() => setPaused(false)}
                    onPause={() => setPaused(true)}
                    // The clip, not the frame: a missing clip falls back to its still.
                    onError={() => setFailed('clip')}
                    className="block size-full object-cover"
                />
                {/* A loop longer than five seconds next to text has to be stoppable. */}
                <button
                    type="button"
                    aria-label={paused ? 'Play the clip' : 'Pause the clip'}
                    onClick={() => {
                        const v = video.current;
                        if (!v) return;
                        if (v.paused) void v.play().catch(() => {});
                        else v.pause();
                    }}
                    className={cn(
                        buttonStyles({ variant: 'secondary', size: 'sm', iconOnly: true }),
                        'absolute bottom-2 right-2 shadow-sm',
                    )}
                >
                    {paused ? <Play className="size-3.5" aria-hidden /> : <Pause className="size-3.5" aria-hidden />}
                </button>
            </figure>
        );
    }

    return (
        <figure className={frame}>
            {/* eslint-disable-next-line @next/next/no-img-element -- the docs site's file, never optimised here */}
            <img
                src={still}
                alt={media.alt}
                width={1440}
                height={900}
                decoding="async"
                onLoad={() => setLoaded(true)}
                onError={() => setFailed('all')}
                // Shown once it has loaded, so a slow or failing load never flashes the alt text.
                className={cn(
                    'block size-full object-cover transition-opacity duration-200 motion-reduce:transition-none',
                    loaded ? 'opacity-100' : 'opacity-0',
                )}
            />
        </figure>
    );
}

function subscribeOnline(onChange: () => void) {
    window.addEventListener('online', onChange);
    window.addEventListener('offline', onChange);
    return () => {
        window.removeEventListener('online', onChange);
        window.removeEventListener('offline', onChange);
    };
}

function useOnline(): boolean {
    return useSyncExternalStore(subscribeOnline, () => navigator.onLine, () => true);
}

const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';

function subscribeReducedMotion(onChange: () => void) {
    const query = window.matchMedia(REDUCED_MOTION);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
}

function useReducedMotion(): boolean {
    return useSyncExternalStore(subscribeReducedMotion, () => window.matchMedia(REDUCED_MOTION).matches, () => false);
}
