import { AppScreen } from '@/components/app/AppScreen';
import { BoardsScreen } from '@/components/boards';
import { OssBoardsHost } from '@/lib/boardsHost';

export const dynamic = 'force-dynamic';

export default async function BoardsPage(props: {
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    // `?board=` opens that board (a workflow's or a schedule's "Open in Boards").
    const searchParams = await props.searchParams;
    const board = typeof searchParams.board === 'string' ? searchParams.board : null;
    return (
        <AppScreen id="boards" width="wide">
            <OssBoardsHost><BoardsScreen initialBoardId={board} /></OssBoardsHost>
        </AppScreen>
    );
}
