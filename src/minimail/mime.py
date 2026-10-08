"""MIME → decoded, searchable fields. The raw bytes are never altered; this only reads them.

Every decoding step degrades instead of failing: a mail with a bogus charset or a broken part must
still be archived and searchable on whatever could be read.
"""

import email
import email.policy
from dataclasses import dataclass, field
from email.message import EmailMessage, Message
from html.parser import HTMLParser

# ponytail: hard cap per message; a multi-MB text body adds nothing a search can use.
MAX_TEXT = 1_000_000

_SKIP_TAGS = frozenset({"script", "style", "head", "title", "noscript", "template"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
)


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text only: tags, attributes, styles and scripts are dropped."""
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except AssertionError:  # pragma: no cover — html.parser's last-resort on pathological markup
        pass
    return " ".join(" ".join(parser.parts).split())


def parse(raw: bytes) -> EmailMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)  # guaranteed by policy.default
    return msg


def part_text(part: Message) -> str:
    """Decoded text of a leaf part: transfer encoding and charset handled, unknown charsets read
    as latin-1 (lossless byte → char) rather than dropped."""
    try:
        content = part.get_content()  # type: ignore[attr-defined]
        # U+FFFD: 8-bit text under a missing or wrong charset (common) — re-read the bytes below.
        if isinstance(content, str) and "\ufffd" not in content:
            return content
    except LookupError, UnicodeError, ValueError, AttributeError:
        pass
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1")


def header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001 — a malformed header must not lose the message
        return ""
    return " ".join(str(value).split()) if value is not None else ""


@dataclass
class Extracted:
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    text: str = ""
    attachments: list[str] = field(default_factory=list)


def body_text(msg: EmailMessage) -> str:
    """The message's own text body (plain preferred, else HTML reduced to its visible text)."""
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    text = part_text(body)
    return html_to_text(text) if body.get_content_type() == "text/html" else text


@dataclass
class Attachment:
    filename: str  # decoded (RFC 2231/2047), unsanitised; "" when the part has none
    content_type: str
    data: bytes  # the part's payload after transfer decoding, byte for byte


class _ForwardedAsLeaf(EmailMessage):
    """Reports `message/rfc822` as a non-`message` type, so the parser keeps a forwarded mail as a
    leaf whose payload is the verbatim bytes the outer MIME carries instead of re-splitting it."""

    def get_content_maintype(self) -> str:
        return (
            "application" if self.get_content_type() == "message/rfc822" else super().get_content_maintype()
        )


_FORWARDED_AS_LEAF = email.policy.default.clone(message_factory=_ForwardedAsLeaf)


def attachments(raw: bytes, depth: int = 0) -> list[Attachment]:
    """Every attachment: a leaf part with a filename or an attachment disposition, or any non-text
    leaf (e.g. an inline image). A forwarded mail is one (`.eml`), followed by its own attachments
    while `depth` < 3 (deeper forwards are still listed, not opened)."""
    found: list[Attachment] = []
    for part in email.message_from_bytes(raw, policy=_FORWARDED_AS_LEAF).walk():
        if part.is_multipart():
            continue
        filename = part.get_filename() or ""
        body_like = part.get_content_maintype() in {"text", "message"}
        if not filename and body_like and part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        if part.get_content_type() != "message/rfc822":
            found.append(Attachment(filename, part.get_content_type(), data))
            continue
        if filename and not filename.lower().endswith(".eml"):
            filename += ".eml"
        found.append(Attachment(filename, "message/rfc822", data))
        # Security: each level copies its nested bytes; unbounded = quadratic memory, RecursionError.
        if depth < 3:
            found.extend(attachments(data, depth + 1))
    return found


def _body_text(msg: EmailMessage, out: list[str], attachments: list[str], depth: int) -> None:
    out.append(body_text(msg))
    for part in _leaves(msg):
        if part.get_content_type() == "message/rfc822":
            # Forwarded mail as attachment: its own headers and body are searchable too.
            inner = part.get_payload(0) if part.is_multipart() else None
            if isinstance(inner, EmailMessage) and depth < 3:
                out.append(header(inner, "subject"))
                out.append(header(inner, "from"))
                _body_text(inner, out, attachments, depth + 1)
            continue
        filename = part.get_filename()
        if filename and part.get_content_disposition() in {"attachment", "inline"}:
            attachments.append(filename)


def _leaves(msg: Message) -> list[Message]:
    """Leaf parts of this message, stopping at embedded messages (walk() would descend into them
    and count a forwarded mail's attachments twice)."""
    if not msg.is_multipart():
        return [msg]  # single-part mail: the part is the message (possibly a bare attachment)
    leaves: list[Message] = []
    for part in msg.get_payload():
        if not isinstance(part, Message):
            continue
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            leaves.extend(_leaves(part))
        else:
            leaves.append(part)
    return leaves


def extract(raw: bytes) -> Extracted:
    try:
        msg = parse(raw)
    except Exception:  # noqa: BLE001 — e.g. RecursionError on absurd nesting: index nothing, keep the MIME
        return Extracted()
    out: list[str] = []
    attachments: list[str] = []
    try:
        _body_text(msg, out, attachments, 0)
    except Exception:  # noqa: BLE001 — keep whatever headers we have; the raw MIME stays intact
        pass
    recipients = ", ".join(h for h in (header(msg, n) for n in ("to", "cc", "bcc")) if h)
    text = "\n".join(t for t in out if t) + ("\n" + " ".join(attachments) if attachments else "")
    return Extracted(
        subject=header(msg, "subject"),
        sender=header(msg, "from"),
        recipients=recipients,
        text=text[:MAX_TEXT],
        attachments=attachments,
    )
