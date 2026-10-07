/*
 * The page frame under its first name.
 *
 * `Screen` was the frame until M1a: title and blurb passed in, one header
 * implementation, each product unpacking its own nav at the call site. It
 * centred its column, though, so a title moved whenever a screen's width did
 * (x = 376 on a form, 288 on a full-width screen). `PageFrame` is the same
 * frame, left-aligned, with an eyebrow line, a tab bar and an aside; `Screen`
 * stays as its other name so no call site — here or in the other product —
 * has to change to get it.
 */
import { PageFrame, type PageWidth } from "./page-frame";

export type ScreenWidth = PageWidth;

export function Screen(props: React.ComponentProps<typeof PageFrame>) {
  return <PageFrame {...props} />;
}
