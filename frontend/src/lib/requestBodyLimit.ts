/**
 * The largest request body this server passes on to the backend (F364).
 *
 * Next copies a request's body for `proxy.ts` and cuts the copy at
 * `proxyClientMaxBodySize` (10 MiB unless set), then sends the cut body on
 * under its original Content-Length. The backend waited for bytes that never
 * came — an import over 10 MB hung for ten minutes — and a route handler read
 * broken JSON: a chat message with 8 MB of images answered 500.
 *
 * 32 MiB holds the largest body the app sends: a message's 20 MiB of images
 * (`core/images.py`, `MAX_TOTAL_BYTES`) as base64 in JSON, about 27 MiB.
 * Knowledge and Vault uploads go in 8 MiB parts (`core/knowledge/ingest.py`,
 * `PART_SIZE`) and never come near it. Above it, `proxy.ts` answers 413 at once.
 *
 * Read by `next.config.ts` at build: a change here takes a rebuild.
 */
export const MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024;
