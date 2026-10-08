"""MIME extraction on a synthetic corpus: every encoding a real mailbox throws at us."""

from email.message import EmailMessage
from typing import Any

import pytest

from minimail import mime
from minimail.mime import MAX_TEXT, extract, html_to_text


def raw(headers: str, body: bytes) -> bytes:
    return headers.strip().replace("\n", "\r\n").encode() + b"\r\n\r\n" + body


def test_quoted_printable_latin1() -> None:
    mime = raw(
        """From: Alice Example <alice@example.test>
To: bob@example.test
Subject: plain
Content-Type: text/plain; charset=iso-8859-1
Content-Transfer-Encoding: quoted-printable""",
        b"R=E9union d'=E9t=E9 =E0 l'h=F4tel",
    )
    ex = extract(mime)
    assert "Réunion d'été à l'hôtel" in ex.text
    assert ex.sender == "Alice Example <alice@example.test>"
    assert ex.recipients == "bob@example.test"


def test_base64_cp1252() -> None:
    import base64

    body = base64.b64encode("Facture 12 € — chef-d'œuvre".encode("cp1252"))
    mime = raw(
        """Subject: x
Content-Type: text/plain; charset=windows-1252
Content-Transfer-Encoding: base64""",
        body,
    )
    assert "Facture 12 € — chef-d'œuvre" in extract(mime).text


def test_rfc2047_subjects() -> None:
    assert extract(raw("Subject: =?utf-8?q?Caf=C3=A9_cr=C3=A8me?=", b"x")).subject == "Café crème"
    assert extract(raw("Subject: =?iso-8859-1?b?6XTp?=", b"x")).subject == "été"


def test_html_only_keeps_visible_text_only() -> None:
    html = (
        b"<html><head><title>T</title><style>.secretstyle{color:red}</style></head>"
        b'<body><p class="hiddenclass" data-x="attrword">Visible &amp; caf&eacute;</p>'
        b"<script>var scriptword = 1;</script><div>second block</div></body></html>"
    )
    text = extract(raw("Subject: h\nContent-Type: text/html; charset=utf-8", html)).text
    assert "Visible & café" in text
    assert "second block" in text
    for hidden in ("secretstyle", "hiddenclass", "attrword", "scriptword", "<p"):
        assert hidden not in text


def test_alternative_prefers_plain() -> None:
    msg = EmailMessage()
    msg["Subject"] = "alt"
    msg.set_content("plain version")
    msg.add_alternative("<p>html version</p>", subtype="html")
    text = extract(msg.as_bytes()).text
    assert "plain version" in text
    assert "html version" not in text


def test_unknown_charset_degrades_to_latin1() -> None:
    mime = raw("Subject: u\nContent-Type: text/plain; charset=x-bogus-charset", b"caf\xe9 ok")
    assert "café ok" in extract(mime).text


def test_forwarded_message_searchable_and_attachments_counted_once() -> None:
    inner = EmailMessage()
    inner["Subject"] = "inner subject"
    inner["From"] = "carol@example.test"
    inner.set_content("inner body")
    inner.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="inner.pdf")
    outer = EmailMessage()
    outer["Subject"] = "fwd"
    outer.set_content("outer body")
    outer.add_attachment(inner)
    outer.add_attachment(b"data", maintype="application", subtype="octet-stream", filename="rapport été.xlsx")
    ex = extract(outer.as_bytes())
    for word in ("outer body", "inner subject", "carol@example.test", "inner body", "rapport été.xlsx"):
        assert word in ex.text
    assert sorted(ex.attachments) == ["inner.pdf", "rapport été.xlsx"]


def test_single_part_attachment_mail() -> None:
    mime = raw(
        """Subject: scan
Content-Type: application/pdf; name="scan.pdf"
Content-Disposition: attachment; filename="scan.pdf"
Content-Transfer-Encoding: base64""",
        b"JVBERg==",
    )
    assert extract(mime).attachments == ["scan.pdf"]


def test_garbage_does_not_raise_and_text_is_capped() -> None:
    assert extract(b"\x00\xff not a mail").subject == ""
    big = raw("Subject: big\nContent-Type: text/plain; charset=utf-8", b"a" * (MAX_TEXT + 10))
    assert len(extract(big).text) == MAX_TEXT


def test_html_to_text_handles_unclosed_markup() -> None:
    assert html_to_text("<div>one<p>two<script>x") == "one two"


def test_undeclared_8bit_utf8_and_latin1() -> None:
    """Undeclared 8-bit body: UTF-8 when it decodes, else latin-1 — never U+FFFD."""
    assert "déjà vu" in extract(raw("Subject: a\nContent-Type: text/plain", "déjà vu".encode())).text
    assert "déjà vu" in extract(raw("Subject: b\nContent-Type: text/plain", "déjà vu".encode("latin-1"))).text
    assert "�" not in extract(raw("Subject: c", b"\xe9t\xe9")).text


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("<p>Bon<b>jour</b> le <span>mon</span>de</p>", "Bonjour le monde"),
        ("<a href='x'>ré</a><i>union</i>", "réunion"),
        ("<p>one</p><p>two</p>", "one two"),
        ("<table><tr><td>cell</td><td>next</td></tr></table>", "cell next"),
        ("<ul><li>a</li><li>b</li></ul>above<hr>below", "a b above below"),
    ],
)
def test_html_to_text_splits_on_blocks_only(html: str, text: str) -> None:
    """001-AC8: inline tags never split a word; block boundaries still separate words."""
    assert html_to_text(html) == text


@pytest.mark.parametrize(
    "html",
    [
        "<html><head><title>T</title><body><p>visible body</p>",
        "<head><meta charset='utf-8'><style>p {}</style>visible body",
    ],
)
def test_html_without_closing_head_still_indexes_the_body(html: str) -> None:
    assert html_to_text(html) == "visible body"


def _mixed(*parts: tuple[str, str]) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "mixed"
    msg.set_content(parts[0][1], subtype=parts[0][0])
    msg.add_attachment(b"\x89PNG", maintype="image", subtype="png", disposition="inline")
    for subtype, text in parts[1:]:
        msg.add_attachment(text, subtype=subtype, disposition="inline")
    return msg.as_bytes()


def test_text_after_an_inline_image_is_indexed_once() -> None:
    """Text, inline image, text (Apple Mail style): every text part is searchable, none twice."""
    text = extract(
        _mixed(("plain", "first words"), ("plain", "after image"), ("html", "<p>tail html</p>"))
    ).text
    assert [text.count(w) for w in ("first words", "after image", "tail html")] == [1, 1, 1]


def test_alternative_body_is_not_counted_twice_and_attachments_stay_out() -> None:
    msg = EmailMessage()
    msg.set_content("alt plain")
    msg.add_alternative("<p>alt html</p>", subtype="html")
    msg.add_attachment("secret notes", filename="notes.txt")  # disposition: attachment
    text = extract(msg.as_bytes()).text
    assert text.count("alt plain") == 1 and "alt html" not in text and "secret notes" not in text


class _Gate:
    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> None:
        self.entered += 1

    def __exit__(self, *exc: object) -> None:
        pass


@pytest.mark.parametrize(("limit", "gated"), [(10, 1), (10_000, 0)])
def test_big_messages_are_parsed_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch, limit: int, gated: int
) -> None:
    gate = _Gate()
    monkeypatch.setattr(mime, "BIG_MIME", limit)
    monkeypatch.setattr(mime, "_BIG_GATE", gate)
    assert extract(raw("Subject: big", b"body words")).text == "body words"
    assert gate.entered == gated


@pytest.mark.parametrize("stage", ["parse", "_body_text"])
def test_extract_failures_are_flagged(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    def boom(*args: Any) -> None:
        raise RecursionError("nested secret")

    monkeypatch.setattr(mime, stage, boom)
    found = extract(raw("Subject: kept", b"body"))
    assert found.error == "RecursionError"
    assert found.subject == ("" if stage == "parse" else "kept")
