"""XML parsing for documents a scanned repository wrote.

Every XML this scanner parses is untrusted: `pom.xml` and `*.csproj` come out of
the repository under audit, and `maven-metadata.xml` comes from a registry
serving what a publisher uploaded. `xml.etree.ElementTree` was used directly for
all of it.

What that does and does not expose, measured on CPython 3.13 rather than assumed:

* **External entities are already refused.** A `<!ENTITY xxe SYSTEM "file:///...">`
  raises `ParseError: undefined entity`, so there is no file disclosure and no
  request smuggled out through a DOCTYPE. The finding that prompted this module
  assumed otherwise.
* **Internal entity expansion is not.** The classic "billion laughs" document
  expands: four nesting levels already produced 30,000 characters from under a
  kilobyte of input, and the growth is exponential in the nesting. A manifest of
  a few kilobytes can therefore ask for more memory than the worker has. Capping
  the input size does not help — the whole point is that the input is small.

So entity declarations are refused outright. Nothing this scanner parses has a
legitimate reason to declare one; Maven POMs, NuGet project files and Maven
metadata are all plain element trees.

`defusedxml` raises its own exception class for a refused document. That is
translated to `ParseError` here so callers keep one thing to catch: every call
site already treats unparseable XML as "no data from this manifest", which is
exactly the right answer for a hostile one too.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _fromstring

__all__ = ["Element", "ParseError", "fromstring"]


def fromstring(text: str) -> Element:
    """Parse XML, refusing entity declarations. Raises ``ParseError`` either way."""
    try:
        return _fromstring(text)
    except DefusedXmlException as exc:
        raise ParseError(f"refused: {type(exc).__name__}") from exc
