"""A small PDF for checking page images (ai/spec/tools.md, "PDF pages and files") on every host.

Pages with a red block at the top left, a blue block at the bottom right and a green
band across the middle (in the page's own coordinates), so a wrong rotation or crop shows:
1. Letter, 612 × 792.
2. Letter rotated 90°.
3. Letter with a crop box (100, 100)–(400, 700).
4. A4 (595.276 × 841.89, not whole points) rotated 180°.
5. Letter with a JPEG 2000 picture (red, green and blue shapes), like the images in scanned PDFs,
   which need the renderer's image decoders.
"""
import base64

PAGES = [
    {"media": (0, 0, 612, 792), "rotate": 0, "crop": None},
    {"media": (0, 0, 612, 792), "rotate": 90, "crop": None},
    {"media": (0, 0, 612, 792), "rotate": 0, "crop": (100, 100, 400, 700)},
    {"media": (0, 0, 595.276, 841.89), "rotate": 180, "crop": None},
    {"media": (0, 0, 612, 792), "rotate": 0, "crop": None, "image": True},
]

# 64 × 64 JPEG 2000 codestream (lossless), made with Pillow/OpenJPEG.
JPX = base64.b64decode(
    "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"
    "UqvSXv/Z"
)


def _num(v):
    return ("%.3f" % v).rstrip("0").rstrip(".")


def _content(media):
    w, h = media[2], media[3]
    return "\n".join([
        "1 0 0 rg 36 %s 160 90 re f" % _num(h - 126),             # red, top left
        "0 0 1 rg %s 36 160 90 re f" % _num(w - 196),             # blue, bottom right
        "0 0.6 0 rg 120 %s %s 40 re f" % (_num(h / 2 - 20), _num(w - 240)),  # green band
        "0 g 2 w 18 18 %s %s re S" % (_num(w - 36), _num(h - 36)),  # frame
    ]).encode()


def make_pdf():
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    for page in PAGES:
        content = _content(page["media"])
        resources = "<< >>"
        if page.get("image"):
            image_id = len(objects) + 1
            objects.append(b"<< /Type /XObject /Subtype /Image /Width 64 /Height 64 /ColorSpace /DeviceRGB /BitsPerComponent 8"
                           b" /Filter /JPXDecode /Length %d >>\nstream\n" % len(JPX) + JPX + b"\nendstream")
            resources = "<< /XObject << /Im1 %d 0 R >> >>" % image_id
            content += b"\nq 400 0 0 400 106 196 cm /Im1 Do Q"
        page_id, content_id = len(objects) + 1, len(objects) + 2
        kids.append(page_id)
        box = lambda b: "[%s]" % " ".join(_num(v) for v in b)
        entries = "/Type /Page /Parent 2 0 R /MediaBox %s /Contents %d 0 R /Resources %s" % (box(page["media"]), content_id, resources)
        if page["rotate"]:
            entries += " /Rotate %d" % page["rotate"]
        if page["crop"]:
            entries += " /CropBox %s" % box(page["crop"])
        objects.append(("<< %s >>" % entries).encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objects[1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % k for k in kids), len(kids))).encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


# (page, region, max_width) cases, and the error cases every host must answer the same way.
RENDERS = [
    (1, None, 600),
    (2, None, 600),
    (3, None, 600),
    (4, None, 600),
    (1, [0.05, 0.05, 0.4, 0.2], 800),
    (2, [0.5, 0.5, 0.6, 0.6], 500),   # clipped to the page
    (3, [0.25, 0.4, 0.5, 0.2], 1000),
    (1, [0.0, 0.0, 0.1, 1.0], 1200),  # tall: height capped at 2000
    (4, None, 5000),                   # max_width clamped to 2000
    (5, None, 600),                    # JPEG 2000 image
    (5, [0.15, 0.25, 0.7, 0.55], 800),
    (1, None, 50),                     # clamped to 200
]
ERRORS = [
    (0, None, 600),
    (6, None, 600),
    (1, [0.5, 0.5, 0, 0.2], 600),
    (1, [1.2, 0, 0.5, 0.5], 600),
    (1, [0.1, 0.2, 0.3], 600),
]


if __name__ == "__main__":
    import sys
    sys.stdout.buffer.write(make_pdf())
