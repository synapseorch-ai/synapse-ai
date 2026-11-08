import { Dashboard } from '@/components/app/Dashboard';
import { docsMcpAnswers } from '@/components/connect/docsMcp';

/**
 * Home. The page draws its own frame: its head is today's date, a greeting and how things stand (M2a). It asks, on the
 * server, whether the docs' MCP server answers, for "Learn Synapse with your AI assistant" (F1030).
 */
export default async function DashboardPage() {
    return <Dashboard docsMcp={await docsMcpAnswers()} />;
}
