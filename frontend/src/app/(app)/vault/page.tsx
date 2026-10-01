import { VaultTab } from '@/components/settings/VaultTab';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

export const dynamic = 'force-dynamic';

export default async function VaultPage(props: {
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    // `?path=` opens that file — an answer's Sources link here (B1b). Read on the
    // server and passed down, as the orchestrations page reads `?run=`.
    const searchParams = await props.searchParams;
    const initialPath = typeof searchParams.path === 'string' ? searchParams.path : undefined;
    const nav = navEntryFor('vault')!;

    return (
        <Screen title={nav.label} description={nav.blurb} bleed>
            <VaultTab initialPath={initialPath} />
        </Screen>
    );
}
