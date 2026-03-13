"""Extract lateral manhole schedule rows from PDF files.

Lateral schedules may include PS/PF chambers, explicit diameter columns,
and size columns.  The rules differ from the main MH schedule.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pdfplumber

from mhls.models import LateralMHScheduleRow
from mhls.pdf_read import get_page_words, try_camelot_stream_then_lattice

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Numeric helpers (shared pattern)
# ---------------------------------------------------------------------------


def _to_float(text: str) -> float | None:
    text = text.strip().replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_mm(text: str) -> int | None:
    """Extract a millimetre value from a cell string."""
    text = text.strip()
    m = re.search(r"(?<!\d)(\d{3,4})(?!\d)", text)
    if m:
        val = int(m.group(1))
        if 300 <= val <= 5000:
            return val
    return None


# ---------------------------------------------------------------------------
# Column detection tokens
# ---------------------------------------------------------------------------

_REF_TOKENS = {"ref", "mh ref", "chamber", "reference", "manhole", "ps", "pf"}
_DIA_TOKENS = {"dia", "size", "mh dia", "chamber size", "internal"}
_COVER_TOKENS = {
    "cover",
    "covel",
    "cl",
    "cover level",
    "cover levels",
    "cover elevation",
    "cover elevations",
}
_INVERT_TOKENS = {
    "invert",
    "il",
    "invert level",
    "invert levels",
    "invert elevation",
    "invert elevations",
    "inv",
    "inv level",
}
_DIAMETER_TOKENS = {"diameter", "pipe dia", "ic dia"}


def _normalise_header(text: str) -> str:
    """Normalise a header cell for token matching.

    Collapses internal whitespace, strips leading/trailing whitespace, and
    lower-cases the result.  This ensures that multi-line PDF cells such as
    ``"COVER\\nLEVELS"`` are treated identically to ``"COVER LEVELS"``.
    """
    return " ".join(text.lower().split())


def _header_matches(normalised: str, tokens: set[str]) -> bool:
    """Return True when *normalised* contains any token as a substring."""
    return any(t in normalised for t in tokens)


def _header_col_indices(
    header_row: list[str],
) -> tuple[int | None, int | None, int | None, list[int], int | None]:
    """Return (ref, dia, cover, invert_cols, diameter) column indices."""
    ref_col: int | None = None
    dia_col: int | None = None
    cover_col: int | None = None
    invert_cols: list[int] = []
    diameter_col: int | None = None

    for i, cell in enumerate(header_row):
        norm = _normalise_header(cell)
        # Check explicit diameter first (more specific)
        if _header_matches(norm, _DIAMETER_TOKENS) and "size" not in norm:
            diameter_col = i
        elif _header_matches(norm, _DIA_TOKENS):
            dia_col = i
        elif _header_matches(norm, _COVER_TOKENS):
            cover_col = i
        elif _header_matches(norm, _INVERT_TOKENS):
            invert_cols.append(i)
        elif _header_matches(norm, _REF_TOKENS):
            ref_col = i

    return ref_col, dia_col, cover_col, invert_cols, diameter_col


def _is_lateral_schedule_page(words: list[dict]) -> bool:
    text = " ".join(w["text"].lower() for w in words)
    has_lateral = "lateral" in text or "schedule" in text
    has_ref = "ref" in text
    has_level = "invert" in text or "cover" in text
    return has_lateral and has_ref and has_level


# ---------------------------------------------------------------------------
# Camelot extraction
# ---------------------------------------------------------------------------


def _from_camelot(
    tables: list[list[list[str]]],
    pdf_name: str,
    page_number: int,
) -> list[LateralMHScheduleRow]:
    rows: list[LateralMHScheduleRow] = []
    for table in tables:
        if len(table) < 2:
            continue
        header_idx = 0
        for i, row in enumerate(table):
            joined = " ".join(row).lower()
            if "ref" in joined and ("invert" in joined or "cover" in joined):
                header_idx = i
                break

        header = table[header_idx]
        ref_col, dia_col, cover_col, invert_cols, diam_col = _header_col_indices(header)

        if ref_col is None:
            continue

        if not invert_cols:
            for j in range(len(header)):
                if j not in (ref_col, dia_col, cover_col, diam_col):
                    invert_cols.append(j)

        for row in table[header_idx + 1 :]:
            if len(row) <= ref_col:
                continue
            mh_ref = row[ref_col].strip()
            if not mh_ref:
                continue

            dia: int | None = None
            if dia_col is not None and dia_col < len(row):
                dia = _parse_mm(row[dia_col])

            cover: float | None = None
            if cover_col is not None and cover_col < len(row):
                cover = _to_float(row[cover_col])

            inv_values: list[float] = []
            for ic in invert_cols:
                if ic < len(row):
                    v = _to_float(row[ic])
                    if v is not None and 0.0 <= v <= 999.0:
                        inv_values.append(v)

            if cover is None and not inv_values:
                log.warning(
                    "pdf=%s page=%d lateral row '%s': missing cover and inverts",
                    pdf_name,
                    page_number,
                    mh_ref,
                )
                continue

            diameter: int | None = None
            if diam_col is not None and diam_col < len(row):
                diameter = _parse_mm(row[diam_col])

            invert = min(inv_values) if inv_values else None
            rows.append(
                LateralMHScheduleRow(
                    mh_ref=mh_ref,
                    mh_dia=dia,
                    cover_level=cover,
                    invert_level=invert,
                    diameter=diameter,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Word fallback extraction
# ---------------------------------------------------------------------------


def _cluster_by_y(words: list[dict], tolerance: float = 4.0) -> dict[float, list[dict]]:
    lines: dict[float, list[dict]] = {}
    for w in sorted(words, key=lambda x: x["top"]):
        y = w["top"]
        placed = False
        for ly in list(lines.keys()):
            if abs(y - ly) <= tolerance:
                lines[ly].append(w)
                placed = True
                break
        if not placed:
            lines[y] = [w]
    return lines


def _from_words(
    words: list[dict],
    pdf_name: str,
    page_number: int,
) -> list[LateralMHScheduleRow]:
    if not words:
        return []

    lines_dict = _cluster_by_y(words)
    lines: list[list[dict]] = []
    for y_key in sorted(lines_dict.keys()):
        lines.append(sorted(lines_dict[y_key], key=lambda w: w["x0"]))

    header_idx = 0
    for i, line in enumerate(lines):
        joined = " ".join(w["text"].lower() for w in line)
        if ("ref" in joined) and ("invert" in joined or "cover" in joined):
            header_idx = i
            break

    header_line = lines[header_idx]
    col_positions = [w["x0"] for w in header_line]
    col_labels = [w["text"].lower() for w in header_line]

    ref_col_x: float | None = None
    dia_col_x: float | None = None
    cover_col_x: float | None = None
    invert_col_xs: list[float] = []
    diam_col_x: float | None = None

    for x, label in zip(col_positions, col_labels, strict=False):
        norm = _normalise_header(label)
        if _header_matches(norm, _DIAMETER_TOKENS) and "size" not in norm:
            diam_col_x = x
        elif _header_matches(norm, _DIA_TOKENS):
            dia_col_x = x
        elif _header_matches(norm, _COVER_TOKENS):
            cover_col_x = x
        elif _header_matches(norm, _INVERT_TOKENS):
            invert_col_xs.append(x)
        elif _header_matches(norm, _REF_TOKENS):
            ref_col_x = x

    if ref_col_x is None:
        return []

    def snap(x: float, tol: float = 20.0) -> float | None:
        for cx in col_positions:
            if abs(x - cx) <= tol:
                return cx
        return None

    rows: list[LateralMHScheduleRow] = []
    for line in lines[header_idx + 1 :]:
        if not line:
            continue
        cell: dict[float, str] = {}
        for w in line:
            cx = snap(w["x0"])
            if cx is not None:
                cell[cx] = cell.get(cx, "") + " " + w["text"]

        mh_ref = cell.get(ref_col_x, "").strip()
        if not mh_ref:
            continue

        dia: int | None = None
        if dia_col_x is not None:
            dia = _parse_mm(cell.get(dia_col_x, ""))

        cover: float | None = None
        if cover_col_x is not None:
            cover = _to_float(cell.get(cover_col_x, ""))

        inv_values: list[float] = []
        for ix in invert_col_xs:
            v = _to_float(cell.get(ix, ""))
            if v is not None and 0.0 <= v <= 999.0:
                inv_values.append(v)

        if cover is None and not inv_values:
            log.warning(
                "pdf=%s page=%d lateral row '%s': missing cover and inverts",
                pdf_name,
                page_number,
                mh_ref,
            )
            continue

        diameter: int | None = None
        if diam_col_x is not None:
            diameter = _parse_mm(cell.get(diam_col_x, ""))

        invert = min(inv_values) if inv_values else None
        rows.append(
            LateralMHScheduleRow(
                mh_ref=mh_ref,
                mh_dia=dia,
                cover_level=cover,
                invert_level=invert,
                diameter=diameter,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_lateral_mh_schedule(
    pdf_path: Path,
    use_ocr: bool = True,
) -> list[LateralMHScheduleRow]:
    """Extract all lateral MH schedule rows from a PDF file."""
    pdf_name = pdf_path.name
    all_rows: list[LateralMHScheduleRow] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                log.info("pdf=%s page=%d extracting lateral MH schedule", pdf_name, page_num)

                camelot_tables = try_camelot_stream_then_lattice(pdf_path, page_num)
                if camelot_tables:
                    rows = _from_camelot(camelot_tables, pdf_name, page_num)
                    if rows:
                        all_rows.extend(rows)
                        continue

                words = get_page_words(page, pdf_path, page_num, use_ocr=use_ocr)
                if not _is_lateral_schedule_page(words):
                    log.debug(
                        "pdf=%s page=%d not a lateral schedule page",
                        pdf_name,
                        page_num,
                    )
                    continue

                rows = _from_words(words, pdf_name, page_num)
                if rows:
                    all_rows.extend(rows)
                else:
                    log.warning(
                        "pdf=%s page=%d no lateral MH rows extracted",
                        pdf_name,
                        page_num,
                    )
    except Exception as exc:
        log.error("pdf=%s failed to parse lateral schedule: %s", pdf_name, exc)

    return all_rows
