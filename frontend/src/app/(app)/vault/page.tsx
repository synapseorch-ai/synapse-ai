import { VaultTab } from '@/components/settings/VaultTab';
import { AppScreen } from '@/components/app/AppScreen';

export const dynamic = 'force-dynamic';

export default async function VaultPage(props: {
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    // `?path=` opens that file — an answer's Sources link here (B1b). Read on the
    // server and passed down, as the orchestrations page reads `?run=`.
    const searchParams = await props.searchParams;
    const initialPath = typeof searchParams.path === 'string' ? searchParams.path : undefined;

    return (
        <AppScreen id="vault" width="wide">
            <VaultTab initialPath={initialPath} />
        </AppScreen>
    );
}
