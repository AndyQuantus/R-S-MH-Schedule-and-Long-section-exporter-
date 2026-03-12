"""Low-level PDF reading helpers.

Provides a unified interface that tries camelot first, then pdfplumber word
extraction, and finally optional OCR via pytesseract.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pdfplumber

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Word-level extraction with pdfplumber
# ---------------------------------------------------------------------------


def extract_words_from_page(page: pdfplumber.page.Page) -> list[dict]:  # type: ignore[name-defined]
    """Return list of word dicts with keys: text, x0, x1, top, bottom."""
    words = page.extract_words(
        x_tolerance=3,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
        extra_attrs=["fontname", "size"],
    )
    return words or []


def page_has_text(page: pdfplumber.page.Page) -> bool:  # type: ignore[name-defined]
    """Return True if the page has any extractable text."""
    chars = page.chars
    return bool(chars)


# ---------------------------------------------------------------------------
# Camelot table extraction
# ---------------------------------------------------------------------------


def try_camelot_tables(
    pdf_path: str | Path,
    page_number: int,  # 1-based
    flavor: str = "lattice",
) -> list[list[list[str]]]:
    """Try to extract tables from a page using camelot.

    Returns a list of tables.  Each table is a list of rows.
    Each row is a list of cell strings.  Returns empty list on failure.
    """
    try:
        import camelot  # type: ignore[import]

        tables = camelot.read_pdf(
            str(pdf_path),
            pages=str(page_number),
            flavor=flavor,
            suppress_stdout=True,
        )
        result: list[list[list[str]]] = []
        for t in tables:
            df = t.df
            rows = [[str(cell).strip() for cell in row] for row in df.values.tolist()]
            result.append(rows)
        return result
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "camelot %s failed for %s page %d: %s",
            flavor,
            pdf_path,
            page_number,
            exc,
        )
        return []


def try_camelot_stream_then_lattice(
    pdf_path: str | Path,
    page_number: int,
) -> list[list[list[str]]]:
    """Try lattice first, then stream."""
    tables = try_camelot_tables(pdf_path, page_number, flavor="lattice")
    if not tables:
        tables = try_camelot_tables(pdf_path, page_number, flavor="stream")
    return tables


# ---------------------------------------------------------------------------
# OCR fallback
# ---------------------------------------------------------------------------


def ocr_page_to_words(
    page: pdfplumber.page.Page,  # type: ignore[name-defined]
    dpi: int = 300,
) -> list[dict]:
    """Use pytesseract to OCR a page image and return word dicts.

    Only called when the page has no text layer.
    Returns the same structure as extract_words_from_page.
    """
    try:
        import pytesseract  # type: ignore[import]
        from PIL import Image  # type: ignore[import]

        img = page.to_image(resolution=dpi).original
        if not isinstance(img, Image.Image):
            img = Image.fromarray(img)  # type: ignore[arg-type]

        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        words: list[dict] = []
        for i, text in enumerate(data["text"]):
            text = text.strip()
            if not text:
                continue
            x = data["left"][i]
            y = data["top"][i]
            w = data["width"][i]
            h = data["height"][i]
            # Scale from pixel to PDF points (72 dpi equivalent)
            scale = 72.0 / dpi
            words.append(
                {
                    "text": text,
                    "x0": x * scale,
                    "x1": (x + w) * scale,
                    "top": y * scale,
                    "bottom": (y + h) * scale,
                    "fontname": "OCR",
                    "size": 10.0,
                }
            )
        return words
    except Exception as exc:  # noqa: BLE001
        log.warning("OCR failed on page: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Unified page words
# ---------------------------------------------------------------------------


def get_page_words(
    page: pdfplumber.page.Page,  # type: ignore[name-defined]
    pdf_path: str | Path,
    page_number: int,
    use_ocr: bool = True,
) -> list[dict]:
    """Get words for a page, falling back to OCR if needed."""
    if page_has_text(page):
        return extract_words_from_page(page)
    if use_ocr:
        log.info(
            "pdf=%s page=%d has no text layer, attempting OCR",
            Path(pdf_path).name,
            page_number,
        )
        return ocr_page_to_words(page)
    log.warning(
        "pdf=%s page=%d has no text layer and OCR disabled",
        Path(pdf_path).name,
        page_number,
    )
    return []
