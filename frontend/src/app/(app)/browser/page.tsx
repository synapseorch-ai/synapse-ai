import { redirect } from 'next/navigation';

/** Agents › Browser opens on its Sessions (BRW2, F1139). */
export default function BrowserPage() {
    redirect('/browser/sessions');
}
