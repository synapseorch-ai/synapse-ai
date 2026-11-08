'use client';
/*
 * The Knowledge page in its hub (C7, BOARD1b): Knowledge · Boards. The synced screen draws its own frame, so the
 * hub's tab bar is handed to it — only while Boards are on, so with them off the page looks as it did before the hub
 * had a second tab (the frame's header changes with a bar at all).
 */
import { KnowledgeScreen } from '@/components/knowledge/KnowledgeScreen';
import { OssKnowledgeHost } from '@/lib/knowledgeHost';
import { AppHubTabs } from './AppHubTabs';
import { useNavFlags } from './NavFlags';

export function KnowledgeHub() {
    const boardsOn = !!useNavFlags().boards_enabled;
    return (
        <OssKnowledgeHost>
            <KnowledgeScreen tabs={boardsOn ? <AppHubTabs hubId="knowledge" current="/knowledge" /> : undefined} />
        </OssKnowledgeHost>
    );
}
