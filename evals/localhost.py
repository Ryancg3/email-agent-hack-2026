"""Guards for the local eval pages, which serve private emails.

Binding to 127.0.0.1 keeps other machines out, but not a website open in your
browser: with DNS rebinding, a site can point its own hostname at 127.0.0.1 and
read the page as a same-origin request. So every request must name this server
as its Host, and a write must come from a page served by it.
"""

from urllib.parse import urlparse


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def is_local_request(headers, port: int, write: bool = False) -> bool:
    """Whether a request is from this server's own page, not from another site."""
    if (headers.get("Host") or "") not in allowed_hosts(port):
        return False
    if write:
        origin = headers.get("Origin")
        # Browsers send Origin on POST; a missing one (curl, tests) has no page to abuse.
        if origin and urlparse(origin).netloc not in allowed_hosts(port):
            return False
    return True
