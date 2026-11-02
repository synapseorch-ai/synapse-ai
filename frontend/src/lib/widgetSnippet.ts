/** The one tag a site carries for a website widget: this install's own address and the widget's public key. */
export const widgetSnippet = (base: string, key: string) =>
    `<script src="${base}/embed/v1.js" data-key="${key}" async></script>`;
