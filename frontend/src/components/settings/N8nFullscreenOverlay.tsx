/* eslint-disable @typescript-eslint/no-explicit-any */

interface N8nFullscreenOverlayProps {
    isIframeFullscreen: boolean;
    toolBuilderMode: 'config' | 'n8n' | 'python';
    draftTool: any;
    setIsIframeFullscreen: (v: boolean) => void;
    getN8nBaseUrl: () => string;
}

export const N8nFullscreenOverlay = ({
    isIframeFullscreen, toolBuilderMode, draftTool,
    setIsIframeFullscreen, getN8nBaseUrl
}: N8nFullscreenOverlayProps) => {
    if (!isIframeFullscreen || toolBuilderMode !== 'n8n' || !draftTool) return null;
    return (
        <div className="fixed inset-0 z-[200] bg-white">
            {/* Exit Fullscreen Button - Bottom position */}
            <button
                onClick={() => setIsIframeFullscreen(false)}
                className="absolute bottom-4 right-4 z-[210] p-3 bg-zinc-900 hover:bg-zinc-800 text-white rounded-md border border-zinc-700 flex items-center gap-2 text-sm font-bold shadow-2xl"
            >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
                Exit Fullscreen
            </button>

            {/* Fullscreen iframe */}
            <iframe
                src={
                    (() => {
                        const base = getN8nBaseUrl();
                        if (draftTool?.workflowId) return `${base}/workflow/${draftTool.workflowId}`;
                        return `${base}/workflow/new`;
                    })()
                }
                className="w-full h-full"
                title="n8n Editor Fullscreen"
                allow="clipboard-read; clipboard-write"
                sandbox="allow-forms allow-modals allow-popups allow-presentation allow-same-origin allow-scripts"
            />
        </div>
    );
};
