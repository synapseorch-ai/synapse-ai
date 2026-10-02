import { KnowledgeScreen } from '@/components/knowledge/KnowledgeScreen';
import { OssKnowledgeHost } from '@/lib/knowledgeHost';

export default function KnowledgePage() {
    return <OssKnowledgeHost><KnowledgeScreen /></OssKnowledgeHost>;
}
