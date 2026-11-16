"""A page that loaded is its content, never "blocked", whatever bot manager watches it (F1633).

The scraper called a page blocked when its HTML held any of a list of markers, whatever the response. Some of those
are scripts the bot managers put on every normal page: Cloudflare's JavaScript detections load
`/cdn-cgi/challenge-platform/scripts/jsd/main.js` on each page of a zone with bot management, PerimeterX's sensor
snippet sets `window._pxAppId`, Imperva's injects `_Incapsula_Resource`, and DataDome's and Akamai's tags ride along too. So
a fully loaded page came back as
"Blocked by anti-bot protection", which made many sites look blocked. Now a page is a refusal only by the signs of a
block page itself (Cloudflare's challenge options, its challenge orchestration, its `cf-mitigated: challenge` header,
DataDome's captcha, PerimeterX's captcha, Imperva's incident page), or by a vendor's routine script on a page that
answered 403, 429 or 503, which then names the vendor.

Each detector here is held by its near misses as well as its hits: a normal page carrying each vendor's routine
script, or a CDN's own headers, is read as content. The pages are small made-up ones carrying each vendor's script or
block shape, never a real site's content.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools import web_scraper


def _response(status=200, headers=None):
    return SimpleNamespace(status=status, headers=headers or {})


def _page(head: str, body: str = "<h1>Partner API</h1><p>How to create a site, with examples.</p>" * 40) -> str:
    return f"<html><head><title>Docs</title>{head}</head><body>{body}</body></html>"


#: Cloudflare's JavaScript detections loader, as a zone with bot management injects it into every page.
_CF_JSD = ("<script>(function(){function c(){var b=a.contentDocument||a.contentWindow.document;if(b){var d=b."
           "createElement('script');d.innerHTML=\"window.__CF$cv$params={r:'9a1b2c3d4e5f6a7b',t:'MTczMTc0NjAwMA=='};"
           "var a=document.createElement('script');a.nonce='';a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';"
           "document.getElementsByTagName('head')[0].appendChild(a);\";b.getElementsByTagName('head')[0].appendChild(d)}}"
           "})();</script>")
_CF_JSD_HASHED = '<script src="/cdn-cgi/challenge-platform/h/b/scripts/jsd/a1b2c3d4e5f6/main.js?"></script>'
_CF_JSD_RAY = '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js?ray=9a1b2c3d4e5f6a7b"></script>'
#: DataDome's and Akamai Bot Manager's routine scripts on a normal page.
_DATADOME_TAG = '<script src="https://js.datadome.co/tags.js" async></script>'
_AKAMAI_JS = '<script type="text/javascript" src="/akam/13/7f3a2b1c" defer></script>'
#: PerimeterX's sensor snippet on a normal page.
_PX_SENSOR = ("<script>(function(){window._pxAppId='PXa1b2c3d4';var p=document.createElement('script');"
              "p.src='//client.perimeterx.net/PXa1b2c3d4/main.min.js';document.head.appendChild(p);})();</script>")
#: Imperva's injected script on a normal page.
_IMPERVA_JS = '<script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3"></script>'


class TestALoadedPageIsItsContent:
    @pytest.mark.parametrize("head", [_CF_JSD, _CF_JSD_HASHED, _CF_JSD_RAY],
                             ids=["cloudflare-jsd", "cloudflare-jsd-hashed", "cloudflare-jsd-ray"])
    def test_a_cloudflare_zones_routine_script(self, head):
        assert web_scraper._refusal(_response(200), _page(head)) is None

    def test_cloudflares_own_headers_on_a_page_that_loaded(self):
        headers = {"server": "cloudflare", "cf-ray": "9a1b2c3d4e5f6a7b-BOM", "cf-cache-status": "DYNAMIC"}
        assert web_scraper._refusal(_response(200, headers), _page(_CF_JSD)) is None

    @pytest.mark.parametrize("head", [_DATADOME_TAG, _AKAMAI_JS], ids=["datadome-tag", "akamai-script"])
    def test_other_bot_managers_routine_scripts(self, head):
        assert web_scraper._refusal(_response(200), _page(head)) is None

    def test_perimeterxs_routine_sensor(self):
        assert web_scraper._refusal(_response(200), _page(_PX_SENSOR)) is None

    def test_impervas_routine_script(self):
        assert web_scraper._refusal(_response(200), _page(_IMPERVA_JS)) is None


class TestABlockPageIsStillRefused:
    def test_a_cloudflare_challenge_page(self):
        html = ("<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={cvId:'3',"
                "cType:'managed'};</script><script src=\"/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=x\">"
                "</script></body></html>")
        assert "Cloudflare" in web_scraper._refusal(_response(403), html)

    def test_cloudflares_challenge_header_names_it(self):
        said = web_scraper._refusal(_response(403, {"cf-mitigated": "challenge"}), "<html><body></body></html>")
        assert said is not None and "Cloudflare" in said, said

    def test_a_routine_script_on_a_refusing_status_names_its_vendor(self):
        said = web_scraper._refusal(_response(403), _page(_PX_SENSOR, "<p>Access denied</p>"))
        assert said is not None and "PerimeterX" in said, said

    def test_a_datadome_captcha(self):
        html = '<html><body><script src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></script></body></html>'
        assert "DataDome" in web_scraper._refusal(_response(200), html)

    def test_a_perimeterx_captcha(self):
        html = ('<html><body><div id="px-captcha"></div><script src="https://captcha.px-cdn.net/PXa1b2c3d4/captcha.js">'
                "</script></body></html>")
        assert "PerimeterX" in web_scraper._refusal(_response(403), html)

    def test_an_imperva_incident_page(self):
        html = ("<html><body><p>Request unsuccessful. Incapsula incident ID: 0-123456789012345678</p>"
                + _IMPERVA_JS + "</body></html>")
        assert "Imperva" in web_scraper._refusal(_response(200), html)

    def test_a_refusing_status_alone(self):
        assert web_scraper._refusal(_response(503), _page(_CF_JSD)) is not None
