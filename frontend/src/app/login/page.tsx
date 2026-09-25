import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

import { LoginForm } from './LoginForm';

/**
 * `redirect` is read on the SERVER and passed down.
 *
 * This page used to call useSearchParams() inside a <Suspense> boundary, which
 * opts that whole subtree out of prerendering — so the initial HTML contained
 * no <form> and no <input> at all, and the sign-in box only appeared once JS
 * had loaded and hydrated.
 */
export default async function LoginPage(props: {
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    const searchParams = await props.searchParams;
    const requested = typeof searchParams.redirect === 'string' ? searchParams.redirect : '/';

    // Only same-origin paths. `//evil.example` is a protocol-relative URL and
    // would otherwise turn this into an open redirect.
    const redirectTo = requested.startsWith('/') && !requested.startsWith('//') ? requested : '/';

    // Which gate this is: a password, or — with login off — the access link
    // (D124). Asked of the backend, which owns both answers.
    let mode: 'password' | 'access' = 'password';
    try {
        const res = await fetch(`${BACKEND_URL}/api/auth/status`, {
            headers: await sessionHeaders(),
            cache: 'no-store',
            signal: AbortSignal.timeout(2000),
        });
        if (res.ok && (await res.json()).access_required === true) mode = 'access';
    } catch {
        // Unreachable backend: show the password form, as before.
    }
    const link = typeof searchParams.link === 'string' ? searchParams.link : undefined;

    return <LoginForm redirectTo={redirectTo} mode={mode} linkProblem={link} />;
}
