import type { Metadata } from "next";
import { Geist_Mono, Inter } from "next/font/google";
// @ts-ignore: CSS module declarations are not present in this repo setup
import "./globals.css";
import { StoreProvider } from "./StoreProvider";

/*
 * Two families, and only two.
 *
 * Inter is the UI font — every label, heading, button and nav item. Geist Mono
 * is for content that is genuinely code: log output, API keys, run ids, JSON.
 * Geist Sans, JetBrains Mono and IBM Plex Sans used to be downloaded here too;
 * two were referenced by nothing at all and the third only by six chat bubbles,
 * which is what made two near-identical sans faces sit side by side.
 */
// With its optical-size axis (14–32): a page title at 28px is drawn from the
// display cut, tighter and finer, and body text from the text cut. The browser
// picks the size from font-size (`font-optical-sizing: auto`).
const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  axes: ["opsz"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Synapse AI",
  description: "Synapse AI",
};

/*
 * The theme, before the first paint (F220).
 *
 * The app shell applies the saved theme after hydration, so a light or system
 * theme used to paint dark first on every load. This runs while <head> is
 * parsed, so the first frame already has `data-theme`, and the `color-scheme`
 * that `tokens.css` sets with it makes scrollbars, selects and date pickers
 * follow. The shell still owns the attribute afterwards: it applies a change,
 * follows the OS and clears it on leaving. The rule is `lib/theme.ts`'s:
 * nothing saved, or `system`, follows the OS.
 *
 * Not on /login (dark by design) or /embed/* (a widget is themed by its own
 * settings, and a `color-scheme` there would stop the iframe being
 * transparent over a light host page). Without the attribute both take the
 * dark tokens and no `color-scheme`.
 */
const THEME_BEFORE_PAINT = `(function () {
  try {
    var path = location.pathname;
    if (path === '/login' || path.indexOf('/login/') === 0 || path.indexOf('/embed/') === 0) return;
    var mode = localStorage.getItem('synapseTheme');
    var light = mode === 'light'
      || (mode !== 'dark' && window.matchMedia('(prefers-color-scheme: light)').matches);
    document.documentElement.setAttribute('data-theme', light ? 'light' : 'dark');
  } catch (e) {}
})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // suppressHydrationWarning: the script below sets an attribute on <html>
    // that the server could not know. It covers this element only.
    <html lang="en" className={`${inter.variable} ${geistMono.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BEFORE_PAINT }} />
      </head>
      <body className="antialiased">
        {/* No NotificationProvider here: it belongs to the app shell,
            (app)/layout.tsx. Up here it also ran on /login and inside every
            widget iframe, where nobody is signed in, and polled the stream
            every 5 s for as long as the page stayed open (F178). */}
        <StoreProvider>
          {children}
        </StoreProvider>
      </body>
    </html>
  );
}
