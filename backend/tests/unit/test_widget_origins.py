"""The origin allow-list is the widget's credential, so it gets the hard cases.

The public key ships in HTML on somebody's marketing page. It is an identifier,
not a secret — which means every one of these tests is about the thing that
actually authorises a conversation: where the page asking is.

The failure mode being guarded against is a *substring* match. `https://acme.com`
must not admit `https://acme.com.evil.net`, and `*.acme.com` must not admit
`https://evilacme.com` or a query string that happens to contain the domain.
"""
import pytest

from core.chat_widgets import origins


class TestNormalise:
    def test_a_full_url_reduces_to_its_origin(self):
        assert origins.normalise("https://Acme.com/pricing?x=1#f") == "https://acme.com"

    def test_a_default_port_is_dropped_so_both_spellings_agree(self):
        assert origins.normalise("https://acme.com:443") == "https://acme.com"
        assert origins.normalise("http://localhost:80") == "http://localhost"

    def test_a_non_default_port_is_part_of_the_origin(self):
        assert origins.normalise("http://localhost:8899") == "http://localhost:8899"

    def test_a_bare_host_is_assumed_https(self):
        assert origins.normalise("acme.com") == "https://acme.com"

    def test_plain_http_is_refused_off_localhost(self):
        """A widget on http:// leaks the whole conversation to the network."""
        assert origins.normalise("http://acme.com") is None
        assert origins.normalise("http://127.0.0.1:3000") == "http://127.0.0.1:3000"

    @pytest.mark.parametrize("value", ["", None, "   ", "ftp://acme.com", "javascript:alert(1)"])
    def test_junk_is_none_rather_than_a_guess(self, value):
        assert origins.normalise(value) is None

    def test_the_null_origin_never_becomes_an_origin(self):
        """A sandboxed iframe and a file:// page both send `Origin: null`.

        Treating that as a matchable value would let anything opened from disk
        into every widget at once.
        """
        assert origins.normalise("null") is None
        assert origins.matches("null", ["https://acme.com"]) is False


class TestMatching:
    def test_an_exact_origin_matches(self):
        assert origins.matches("https://acme.com", ["https://acme.com"]) is True

    def test_a_suffix_impostor_is_refused(self):
        """The whole reason matching is on parsed origins and not on strings."""
        assert origins.matches("https://acme.com.evil.net", ["https://acme.com"]) is False

    def test_a_prefix_impostor_is_refused(self):
        assert origins.matches("https://notacme.com", ["https://acme.com"]) is False

    def test_scheme_must_agree(self):
        assert origins.matches("http://localhost", ["https://localhost"]) is False

    def test_port_must_agree(self):
        assert origins.matches("http://localhost:9999", ["http://localhost:8899"]) is False

    def test_an_empty_list_admits_nothing(self):
        """The opposite of this codebase's usual 'unset means unlimited', and
        deliberately so: this default faces the open internet."""
        assert origins.matches("https://acme.com", []) is False
        assert origins.matches("https://acme.com", None) is False

    def test_one_entry_does_not_admit_its_neighbours(self):
        allowed = ["https://acme.com", "https://shop.acme.com"]
        assert origins.matches("https://acme.com", allowed) is True
        assert origins.matches("https://blog.acme.com", allowed) is False


class TestWildcards:
    def test_a_subdomain_wildcard_matches_a_subdomain(self):
        assert origins.matches("https://shop.acme.com", ["*.acme.com"]) is True

    def test_it_matches_deeper_subdomains_too(self):
        assert origins.matches("https://eu.shop.acme.com", ["*.acme.com"]) is True

    def test_it_does_not_match_the_bare_domain(self):
        """`*.acme.com` is a statement about subdomains. Someone who wants both
        lists both, and that is clearer than a wildcard that quietly widens."""
        assert origins.matches("https://acme.com", ["*.acme.com"]) is False

    def test_it_matches_on_the_dot_and_not_on_the_letters(self):
        assert origins.matches("https://evilacme.com", ["*.acme.com"]) is False

    def test_a_wildcard_still_respects_scheme(self):
        assert origins.matches("http://shop.acme.com", ["*.acme.com"]) is False


class TestTheStoredList:
    def test_unparseable_entries_are_dropped_not_stored(self):
        """A typo must not sit in the list looking like it works."""
        assert origins.clean_list(["https://acme.com", "not a url", "", None]) == [
            "https://acme.com"
        ]

    def test_duplicates_collapse_and_order_is_kept(self):
        assert origins.clean_list(
            ["https://b.com", "https://a.com", "https://B.com:443/"]
        ) == ["https://b.com", "https://a.com"]

    def test_a_wildcard_survives_normalisation(self):
        assert origins.clean_list(["*.acme.com"]) == ["https://*.acme.com"]


class TestFrameAncestors:
    def test_an_empty_list_is_none_not_absent(self):
        """`frame-ancestors 'none'` is what makes an unconfigured widget
        unembeddable. Omitting the header entirely would make it embeddable
        everywhere — the exact inversion this feature cannot afford."""
        assert origins.frame_ancestors([]) == "frame-ancestors 'none'"

    def test_it_lists_every_allowed_origin(self):
        directive = origins.frame_ancestors(["https://acme.com", "*.acme.com"])
        assert directive == "frame-ancestors https://acme.com https://*.acme.com"
