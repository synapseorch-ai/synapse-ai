/**
 * Where a press on the browser's picture falls on the page (BRW2, F1197). The picture is drawn whole inside its box
 * (`object-contain`): scaled by the smaller of the two fits and centred, so a box that isn't the page's shape has
 * bars beside or above it. A point is placed through the picture as drawn, never across the whole box (which missed
 * by up to ~70 px near the edges, MEDIA1 10-30); a point in a bar is off the page, and none is given.
 *
 * `box` is the image's content box in client pixels (its border left out); `picture` the image's own size, whose shape
 * is what is drawn; `page` the page's size in its own pixels, what a press is sent in (a frame's `w`/`h`).
 */
export type Box = { left: number; top: number; width: number; height: number };
export type Size = { width: number; height: number };

export function pagePoint(box: Box, picture: Size, page: Size, client: { x: number; y: number }): { x: number; y: number } | null {
  if (box.width <= 0 || box.height <= 0 || picture.width <= 0 || picture.height <= 0) return null;
  const fit = Math.min(box.width / picture.width, box.height / picture.height);
  const drawnW = picture.width * fit;
  const drawnH = picture.height * fit;
  const fx = (client.x - box.left - (box.width - drawnW) / 2) / drawnW;
  const fy = (client.y - box.top - (box.height - drawnH) / 2) / drawnH;
  if (fx < 0 || fy < 0 || fx > 1 || fy > 1) return null;
  const w = page.width || picture.width;
  const h = page.height || picture.height;
  return { x: Math.min(w - 1, Math.round(fx * w)), y: Math.min(h - 1, Math.round(fy * h)) };
}
