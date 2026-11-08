import { OssPhone } from '@/lib/phoneHost';

export const dynamic = 'force-dynamic';

/*
 * Phone: the Twilio numbers an agent answers, a tab of the Channels hub (V29,
 * VOX1c). `?number=` opens a number and `?call=` that call's log over it: the
 * bell's voicemail, transfer and approval items link here. Keyed by both, so a
 * link that changes them opens what it names.
 */
export default async function PhonePage(props: {
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    const searchParams = await props.searchParams;
    const number = typeof searchParams.number === 'string' ? searchParams.number : undefined;
    const call = typeof searchParams.call === 'string' ? searchParams.call : undefined;
    return <OssPhone key={`${number ?? ''}:${call ?? ''}`} number={number} call={call} />;
}
