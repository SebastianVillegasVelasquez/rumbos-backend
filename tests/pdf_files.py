"""PDFs made at test time, byte by byte, so a test can plant exactly the
construct it is about (a JavaScript action, a launch action, an embedded file,
an object stream, a lying page count...) without a fixture file or a PDF
writer that would refuse to produce it."""

import io
import struct
import zlib

from pypdf import PdfReader, PdfWriter

# Object numbers of a document: 1 catalog, 2 page tree, then a page and its
# content for each page; `extra` objects follow.
CATALOG, PAGES = 1, 2


def first_extra(pages: int = 1) -> int:
    """The number of the first object passed as `extra` (and the ones after)."""
    return 3 + 2 * pages


def build_pdf(
    *,
    pages: int = 1,
    catalog: str = "",
    page: str = "",
    extra: tuple[str, ...] = (),
    trailer: str = "",
    count: int | None = None,
    pages_dict: str = "",
    header: bytes = b"%PDF-1.7\n",
) -> bytes:
    """A small valid PDF. `catalog`, `page` and `trailer` are inserted into
    the dictionaries of that name; `extra` are more objects (numbered from
    `first_extra(pages)`); `count` overrides the `/Count` of the page tree."""
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages))
    declared = pages if count is None else count
    bodies: list[bytes] = [
        f"<< /Type /Catalog /Pages 2 0 R {catalog} >>".encode(),
        f"<< /Type /Pages /Kids [{kids}] /Count {declared} {pages_dict} >>".encode(),
    ]
    for i in range(pages):
        content = 4 + 2 * i
        bodies.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Contents {content} 0 R {page} >>".encode()
        )
        stream = b"BT /F1 12 Tf 20 100 Td (Hola) Tj ET"
        bodies.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    bodies.extend(body.encode("latin-1") for body in extra)

    out = bytearray(header + b"%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R {trailer} >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def build_pdf_with_object_stream(hidden: str, *, catalog: str = "") -> bytes:
    """A PDF 1.5 file whose object 5 (`hidden`) lives inside a compressed object
    stream, found through a cross-reference stream: the form most PDF writers
    produce, and the one in which a table-only scan sees nothing. The catalog
    may refer to `5 0 R`."""
    plain = {
        1: f"<< /Type /Catalog /Pages 2 0 R {catalog} >>".encode(),
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
        4: b"<< /Length 4 >>\nstream\nq Q \nendstream",
    }
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number, body in plain.items():
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    head = b"5 0 "
    packed = zlib.compress(head + hidden.encode("latin-1"))
    offsets[6] = len(out)
    out += (
        f"6 0 obj\n<< /Type /ObjStm /N 1 /First {len(head)} "
        f"/Filter /FlateDecode /Length {len(packed)} >>\nstream\n"
    ).encode()
    out += packed + b"\nendstream\nendobj\n"

    offsets[7] = len(out)
    rows = [struct.pack(">BIH", 0, 0, 65535)]
    rows += [struct.pack(">BIH", 1, offsets[n], 0) for n in (1, 2, 3, 4)]
    rows.append(struct.pack(">BIH", 2, 6, 0))  # object 5: in stream 6, slot 0
    rows += [struct.pack(">BIH", 1, offsets[n], 0) for n in (6, 7)]
    table = zlib.compress(b"".join(rows))
    out += (
        f"7 0 obj\n<< /Type /XRef /Size 8 /W [1 4 2] /Root 1 0 R "
        f"/Filter /FlateDecode /Length {len(table)} >>\nstream\n"
    ).encode()
    out += table + b"\nendstream\nendobj\n"
    out += f"startxref\n{offsets[7]}\n%%EOF\n".encode()
    return bytes(out)


def encrypted(pdf: bytes, password: str = "secret") -> bytes:
    """`pdf` with AES encryption (a password to open it)."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.encrypt(password, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
