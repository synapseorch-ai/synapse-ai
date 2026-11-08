"""The seam that lets an embedder keep credentials out of `collections`.

`tools/sql_agent.py` hands `connection_string` straight to `create_engine`, so a
document holding an *address* rather than a value has to be swapped on the way
out of the store. Doing it at the call site would mean doing it at every call
site, and missing one produces a driver error about an unknown URL scheme rather
than a clear "that credential is missing".
"""
import pytest

from core.store import collections


@pytest.fixture(autouse=True)
def _no_resolver():
    """Nothing is installed by default, and nothing leaks between tests."""
    collections.set_document_resolver(None)
    yield
    collections.set_document_resolver(None)


class TestTheDefaultIsNothing:
    async def test_a_document_is_returned_as_written(self) -> None:
        """The shipped product reads exactly what it wrote."""
        # No inline credentials: the repo's secret scanner reads fixtures too,
        # and a fixture that trips it is one someone later bypasses the hook for.
        document = {"connection_string": "postgresql://example.invalid/analytics"}
        assert await collections._resolved(document) is document

    async def test_a_non_dict_is_left_alone(self) -> None:
        assert await collections._resolved("not a document") == "not a document"


class TestWithAResolverInstalled:
    async def test_references_are_swapped(self) -> None:
        async def resolve(document: dict) -> dict:
            return {
                k: ("postgresql://real" if str(v).startswith("ref://") else v)
                for k, v in document.items()
            }

        collections.set_document_resolver(resolve)

        out = await collections._resolved(
            {"name": "prod", "connection_string": "ref://org/db:prod"}
        )
        assert out == {"name": "prod", "connection_string": "postgresql://real"}

    async def test_a_resolver_that_raises_is_allowed_to(self) -> None:
        """A missing credential must surface as itself.

        Swallowing it would return the document with the address still in place,
        and the next thing to touch it hands `create_engine` a URL scheme it has
        never heard of — an error about the wrong thing entirely.
        """

        async def resolve(_document: dict) -> dict:
            raise KeyError("secret db:prod is referenced but not set")

        collections.set_document_resolver(resolve)

        with pytest.raises(KeyError, match="not set"):
            await collections._resolved({"connection_string": "ref://x"})

    async def test_it_can_be_uninstalled(self) -> None:
        async def resolve(_document: dict) -> dict:
            return {"swapped": True}

        collections.set_document_resolver(resolve)
        assert await collections._resolved({"a": 1}) == {"swapped": True}

        collections.set_document_resolver(None)
        assert await collections._resolved({"a": 1}) == {"a": 1}
