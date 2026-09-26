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
const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Synapse AI",
  description: "Synapse AI",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${inter.variable} ${geistMono.variable}`}>
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
