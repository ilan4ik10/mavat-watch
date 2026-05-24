from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

import pymupdf

GREEN_HIGHLIGHT = (0.55, 1.0, 0.55)
GREEN_UNDERLINE = (0.0, 0.6, 0.0)


def _extract_words(path):
    doc = pymupdf.open(str(path))
    out = []
    for i in range(doc.page_count):
        for w in doc[i].get_text("words"):
            out.append((i, w[4], (w[0], w[1], w[2], w[3])))
    doc.close()
    return out


def _is_vertical(bbox):
    return (bbox[3] - bbox[1]) > 1.5 * (bbox[2] - bbox[0])


def _quad(bbox):
    return pymupdf.Quad(
        pymupdf.Point(bbox[0], bbox[1]), pymupdf.Point(bbox[2], bbox[1]),
        pymupdf.Point(bbox[0], bbox[3]), pymupdf.Point(bbox[2], bbox[3]),
    )


def make_highlighted_pdf(old_path, new_path, out_path) -> int:
    """Write a copy of `new_path` to `out_path` with green highlights on
    words that were added/changed relative to `old_path`.
    Returns the number of highlighted words (0 if no differences)."""
    old_words = _extract_words(old_path)
    new_words = _extract_words(new_path)
    sm = difflib.SequenceMatcher(
        a=[w[1] for w in old_words], b=[w[1] for w in new_words], autojunk=False,
    )
    added = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new_words[j1:j2])
    if not added:
        return 0

    out_path = Path(out_path)
    shutil.copy(new_path, out_path)
    doc = pymupdf.open(str(out_path))
    for pi, _, bbox in added:
        page = doc[pi]
        if _is_vertical(bbox):
            annot = page.add_highlight_annot(_quad(bbox))
            annot.set_colors(stroke=GREEN_HIGHLIGHT)
        else:
            annot = page.add_underline_annot(_quad(bbox))
            annot.set_colors(stroke=GREEN_UNDERLINE)
        annot.update()
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    doc.save(str(tmp), deflate=True, garbage=4)
    doc.close()
    tmp.replace(out_path)
    return len(added)


_NUMERIC_TOKEN = re.compile(r"^[\d,]{3,}$")


def mutate_one_number(pdf_path, replacement: str = "9,999") -> bool:
    """Replace the first numeric token in `pdf_path` (in place) with `replacement`.
    Returns True if a token was replaced. For demo purposes only."""
    pdf_path = Path(pdf_path)
    doc = pymupdf.open(str(pdf_path))
    hit = None
    for page in doc:
        for w in page.get_text("words"):
            txt = w[4]
            if _NUMERIC_TOKEN.match(txt) and any(c.isdigit() for c in txt):
                hit = (page, (w[0], w[1], w[2], w[3]))
                break
        if hit:
            break
    if not hit:
        doc.close()
        return False
    page, bbox = hit
    page.add_redact_annot(pymupdf.Rect(*bbox))
    page.apply_redactions()
    is_vert = (bbox[3] - bbox[1]) > 1.5 * (bbox[2] - bbox[0])
    if is_vert:
        cx = (bbox[0] + bbox[2]) / 2
        page.insert_text((cx, bbox[3]), replacement, fontname="helv", fontsize=10, rotate=90)
    else:
        page.insert_text((bbox[0], bbox[3] - 2), replacement, fontname="helv", fontsize=10)
    tmp = pdf_path.with_suffix(pdf_path.suffix + ".tmp")
    doc.save(str(tmp), deflate=True, garbage=4)
    doc.close()
    tmp.replace(pdf_path)
    return True
