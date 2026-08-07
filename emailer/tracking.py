"""
Open tracking — the invisible 1x1 pixel and the ID that links a pixel hit
back to a specific send.

Read this before trusting the numbers it produces:

- **Opens are inflated.** Apple Mail Privacy Protection pre-fetches every
  remote image the moment mail arrives, whether or not a human ever looks at
  it, and it does so from Apple's relay with an ordinary-looking user agent.
  There is no reliable way to tell that apart from a real open. Corporate
  security scanners do the same. `looks_automated()` below catches only the
  obvious non-humans; the rest are indistinguishable and will be counted.
- **Opens are also deflated.** A recipient who reads the mail with images
  turned off registers nothing at all.

So treat this as a trend line, never as a count of people. A reply is the
only signal that is both accurate and actually worth something — it's what
moves sender reputation, which an open does not.

The tracking ID is derived from the message's RFC Message-ID rather than
stored alongside it. That keeps the pixel out of the send path's return
values: the sender builds the pixel from the ID it already generated, and
app.py recomputes the same digest from the message_id it already persists.
No extra column, no plumbing, and the two can't fall out of sync.
"""

import hashlib

import config

# A 1x1 fully transparent GIF — the smallest valid image that renders as
# nothing. 43 bytes.
TRANSPARENT_GIF = bytes([
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00, 0x80, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x21, 0xF9, 0x04, 0x01, 0x00,
    0x00, 0x00, 0x00, 0x2C, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00,
    0x00, 0x02, 0x02, 0x44, 0x01, 0x00, 0x3B,
])

# Substrings that identify a fetcher no human is sitting behind. Deliberately
# short: a false "automated" verdict hides a real open, which is worse than
# letting an unknown agent through and counting it.
_AUTOMATED_AGENTS = (
    "curl", "wget", "python-requests", "httpx", "go-http-client", "java/",
    "libwww", "scrapy", "bot", "crawler", "spider", "monitor", "scanner",
    "proofpoint", "barracuda", "mimecast", "symantec", "microsoft office",
)


def tracking_id_for(message_id: str) -> str:
    """
    Stable, unguessable ID for a sent message.

    Derived from the Message-ID so any component holding one can compute the
    other without a lookup. Not reversible — the open endpoint matches it
    against IDs computed from the send log, so a stranger can't forge a hit
    for a message they haven't seen.
    """
    if not message_id:
        return ""
    return hashlib.blake2s(message_id.encode("utf-8"), digest_size=9).hexdigest()


def pixel_url(tracking_id: str) -> str:
    """
    Absolute URL of the tracking pixel, or "" if it can't be built.

    Requires APP_BASE_URL: a relative path is meaningless inside an email,
    since there's no page for the mail client to resolve it against.
    """
    if not tracking_id or not config.APP_BASE_URL:
        return ""
    return f"{config.APP_BASE_URL}/o/{tracking_id}.gif"


def pixel_html(tracking_id: str) -> str:
    """
    The <img> tag to append to an HTML body, or "" if tracking is off or
    unconfigured.

    No `display:none` and no zero dimensions — hiding an image is itself a
    spam heuristic, and a genuine 1x1 is already invisible. The alt text is
    empty so screen readers skip it rather than announcing a stray image.
    """
    if not config.EMAIL_OPEN_TRACKING:
        return ""

    url = pixel_url(tracking_id)
    if not url:
        return ""

    return f'<img src="{url}" width="1" height="1" border="0" alt="">'


def looks_automated(user_agent: str) -> bool:
    """
    True if the fetch obviously came from a machine rather than a person
    opening mail.

    Note what this does NOT catch: Apple Mail Privacy Protection, which is
    the single largest source of false opens, is designed to be
    indistinguishable from a real client. Gmail's own image proxy
    (GoogleImageProxy) is deliberately not listed here either — Gmail fetches
    through it when a user actually opens the message, so treating it as
    automated would discard most of the real signal this tool is after.
    """
    agent = (user_agent or "").lower()
    if not agent:
        # An empty user agent is far more often a script than a mail client,
        # but it isn't proof, so it counts as automated rather than nothing.
        return True
    return any(marker in agent for marker in _AUTOMATED_AGENTS)


def hash_ip(ip: str) -> str:
    """
    Short digest of the requesting IP, for spotting repeat fetches without
    keeping an identifiable address on disk.
    """
    if not ip:
        return ""
    return hashlib.blake2s(ip.encode("utf-8"), digest_size=8).hexdigest()
