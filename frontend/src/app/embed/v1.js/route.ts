import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

/**
 * The one tag.
 *
 *   <script src="https://your-install/embed/v1.js" data-key="pk_…" async></script>
 *
 * Everything this file does is create an iframe and talk to it. It deliberately
 * does **not** render the chat into the host page:
 *
 * - The host page cannot read the conversation or the visitor token, because the
 *   iframe is a different browsing context it has no access into. A widget drawn
 *   into the host's own DOM gives both away to any script on that page.
 * - The customer's CSS cannot leak into the chat, and ours cannot leak out. No
 *   shadow-DOM gymnastics, no reset stylesheet, no specificity war.
 * - The chat inside is same-origin with our own app, so it reaches the API the
 *   ordinary way. There is no CORS surface anywhere in this feature, which is
 *   the property that kept it small.
 *
 * Kept in plain hand-written JS with no build step, because it is served to
 * every visitor of every customer site: it should stay something a person can
 * read in one sitting and a browser can parse in no time.
 */
const LOADER = String.raw`
(function () {
  var script = document.currentScript;
  if (!script) {
    var all = document.getElementsByTagName('script');
    for (var i = all.length - 1; i >= 0; i--) {
      if (all[i].src && all[i].src.indexOf('/embed/v1.js') !== -1) { script = all[i]; break; }
    }
  }
  if (!script) return;

  var key = script.getAttribute('data-key');
  if (!key) {
    // Said out loud rather than failing silently: the person who just pasted the
    // snippet is the person reading this console, and a widget that renders
    // nothing with no explanation is the worst version of this bug.
    console.error('[synapse] the <script> tag needs a data-key attribute.');
    return;
  }

  var origin = new URL(script.src, window.location.href).origin;
  var mountSelector = script.getAttribute('data-mount');
  var position = script.getAttribute('data-position') || 'right';
  var label = script.getAttribute('data-label') || 'Chat';
  var id = 'synapse-widget-' + Math.random().toString(36).slice(2, 8);

  // Whether the frame ever managed to render. See watchForABlockedFrame.
  var ready = false;

  /**
   * Say out loud when the browser refused to paint the widget.
   *
   * If this page's origin is not on the widget's allow-list, the embed page
   * comes back with a frame-ancestors directive that excludes it, and the
   * browser blocks the frame outright. That is the strongest refusal there is
   * and a completely silent one: no error, no event, just an empty box.
   * Nothing can read into a blocked frame to find out why, so the signal is
   * its absence: the frame announces itself when it renders, and not hearing
   * that means it never did.
   *
   * The console is the right place for it. The reader is a developer who has
   * just pasted a snippet, and the fix is one line of configuration.
   */
  function watchForABlockedFrame() {
    setTimeout(function () {
      if (ready) return;
      console.error(
        '[synapse] the chat widget did not load on ' + window.location.origin + '.\n' +
        'The most likely reason is that this site is not on the widget\'s allowed list, ' +
        'so the browser refused to embed it. Add ' + window.location.origin +
        ' under Settings \u2192 Widgets, or check that data-key is the current key.'
      );
    }, 6000);
  }

  var frame = document.createElement('iframe');
  frame.src = origin + '/embed/' + encodeURIComponent(key);
  frame.title = label;
  frame.setAttribute('aria-label', label);
  frame.allow = 'clipboard-write';
  // allow-same-origin is required and is safe here. Required, because without it
  // the frame gets an opaque origin and its own fetches to our API become
  // cross-origin — which would drag in the CORS surface this design exists to
  // avoid. Safe, because the frame is same-origin with *us*, never with the host
  // page: the usual warning about allow-same-origin plus allow-scripts is about
  // sandboxing a document from the site serving it, which is not this.
  //
  // What is deliberately withheld is allow-top-navigation: a chat widget must
  // never be able to move the page it is embedded in.
  frame.setAttribute('sandbox', 'allow-same-origin allow-scripts allow-forms allow-popups');
  frame.style.border = '0';
  frame.style.colorScheme = 'normal';

  if (mountSelector) {
    // Inline: the customer decides where it goes and how big it is.
    var host = document.querySelector(mountSelector);
    if (!host) {
      console.error('[synapse] no element matches data-mount="' + mountSelector + '".');
      return;
    }
    frame.id = id;
    frame.style.width = '100%';
    frame.style.height = '100%';
    frame.style.minHeight = '420px';
    host.appendChild(frame);
    watchForABlockedFrame();
    return;
  }

  // Bubble: a launcher in the corner, and a panel that opens above it.
  var open = false;
  frame.id = id;
  frame.style.position = 'fixed';
  frame.style.zIndex = '2147483000';
  frame.style.bottom = '96px';
  frame.style[position === 'left' ? 'left' : 'right'] = '20px';
  frame.style.width = 'min(400px, calc(100vw - 40px))';
  frame.style.height = 'min(620px, calc(100vh - 130px))';
  frame.style.borderRadius = '14px';
  frame.style.boxShadow = '0 12px 40px rgba(0,0,0,.18)';
  frame.style.display = 'none';
  frame.style.background = 'transparent';

  var button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('aria-label', label);
  button.setAttribute('aria-expanded', 'false');
  button.style.cssText = [
    'position:fixed', 'z-index:2147483001', 'bottom:20px',
    (position === 'left' ? 'left' : 'right') + ':20px',
    'width:56px', 'height:56px', 'border-radius:28px', 'border:0',
    'cursor:pointer', 'background:#4f46e5', 'color:#fff',
    'box-shadow:0 6px 20px rgba(0,0,0,.22)',
    'display:flex', 'align-items:center', 'justify-content:center',
    'padding:0', 'font:inherit'
  ].join(';');
  button.innerHTML =
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';

  function setOpen(next) {
    open = next;
    frame.style.display = open ? 'block' : 'none';
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) frame.contentWindow && frame.contentWindow.postMessage({ type: 'synapse:opened' }, origin);
  }
  button.addEventListener('click', function () { setOpen(!open); });

  // The frame asks to be closed rather than reaching into the host page, which
  // it cannot do anyway. Checked against our own origin: a message from anywhere
  // else is somebody else's page talking, and is ignored.
  window.addEventListener('message', function (event) {
    if (event.origin !== origin || !event.data) return;
    if (event.data.type === 'synapse:ready') ready = true;
    if (event.data.type === 'synapse:close') setOpen(false);
  });

  function attach() {
    document.body.appendChild(frame);
    document.body.appendChild(button);
    watchForABlockedFrame();
  }
  if (document.body) attach();
  else document.addEventListener('DOMContentLoaded', attach);
})();
`;

export async function GET() {
  return new NextResponse(LOADER, {
    headers: {
      'Content-Type': 'application/javascript; charset=utf-8',
      // Short, because the snippet is the product's public contract and a bad
      // one should be fixable in minutes rather than a day.
      'Cache-Control': 'public, max-age=300',
    },
  });
}
