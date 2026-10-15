import { AppHubTabs } from '@/components/app/AppHubTabs';
import { KnowledgeScreen } from '@/components/knowledge/KnowledgeScreen';
import { OssKnowledgeHost } from '@/lib/knowledgeHost';

export default function KnowledgePage() {
    // Knowledge · Boards (C7, BOARD1b): the screen draws its own frame, so the hub's tab bar is handed to it.
    return <OssKnowledgeHost><KnowledgeScreen tabs={<AppHubTabs hubId="knowledge" current="/knowledge" />} /></OssKnowledgeHost>;
}
