/**
 * Links that read as links.
 *
 * `text-accent hover:underline` was written out nine times, `text-text-muted
 * hover:text-text` five more, and the two drifted: some underlined on hover,
 * some changed colour, one did both. Two variants replace all of it.
 *
 * The underline appears on focus as well as hover. A keyboard user tabbing
 * through a table of "Respond" links otherwise has only the focus ring to tell
 * them where they are, and the ring is the weaker signal of the two.
 */

import Link from "next/link";
import * as React from "react";

import { cn } from "@/lib/cn";

export type TextLinkVariant = "accent" | "quiet" | "plain";

const VARIANTS: Record<TextLinkVariant, string> = {
  /** The action in a row, the "sign up instead" under a form. */
  accent: "font-medium text-accent-ink hover:underline focus-visible:underline",
  /** Navigation and back-links: present, but not competing with the content. */
  quiet: "text-text-muted transition-colors hover:text-text focus-visible:text-text",
  /** Inherits its colour — for a link inside a sentence that is already styled. */
  plain: "underline underline-offset-2 hover:text-text",
};

export function TextLink({
  children,
  href,
  variant = "accent",
  external = false,
  className,
  ...props
}: Omit<React.ComponentPropsWithoutRef<typeof Link>, "href"> & {
  href: string;
  variant?: TextLinkVariant;
  /** Renders a plain `<a>` — for another subdomain, where the router cannot help. */
  external?: boolean;
}) {
  const classes = cn(
    "rounded-sm underline-offset-2",
    VARIANTS[variant],
    className,
  );

  if (external) {
    return (
      <a
        href={href}
        className={classes}
        {...(props as React.AnchorHTMLAttributes<HTMLAnchorElement>)}
      >
        {children}
      </a>
    );
  }

  return (
    <Link href={href} className={classes} {...props}>
      {children}
    </Link>
  );
}
