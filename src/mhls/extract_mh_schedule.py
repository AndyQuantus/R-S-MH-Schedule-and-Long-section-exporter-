"""Extract manhole schedule rows from PDF files.

Supports both camelot table extraction and pdfplumber word-level fallback.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pdfplumber

from mhls.models import MHScheduleRow
from mhls.pdf_read import get_page_words, try_camelot_stream_then_lattice

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

_DECIMAL_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_INTEGER_RE = re.compile(r"^\d{3,5}$")


def _to_float(text: str) -> float | None:
    """Convert a string to float, handling comma-as-decimal."""
    text = text.strip().replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(text: str) -> int | None:
    """Convert a string to int."""
    text = text.strip().replace(",", "")
    try:
        return int(text)
    except ValueError:
        return None


def _parse_dia(text: str) -> int | None:
    """Extract a chamber size in mm from a cell string."""
    text = text.strip()
    # Standard UK chamber sizes
    m = re.search(r"(?<!\d)(900|1050|1200|1350|1500|1800|2100|2400|3000|450|600|675)(?!\d)", text)
    if m:
        return int(m.group(1))
    # fallback: any 3-4 digit number that looks like mm
    m2 = re.search(r"(?<!\d)(\d{3,4})(?!\d)", text)
    if m2:
        val = int(m2.group(1))
        if 300 <= val <= 5000:
            return val
    return None


def _all_inverts_from_row(cells: list[str]) -> list[float]:
    """Collect all numeric values that might be invert levels from a row."""
    inverts: list[float] = []
    for cell in cells:
        v = _to_float(cell)
        if v is not None and 0.0 <= v <= 999.0:
            inverts.append(v)
    return inverts


# ---------------------------------------------------------------------------
# Column index detection
# ---------------------------------------------------------------------------

_MH_REF_TOKENS = {"mh", "ref", "mh ref", "chamber", "reference", "manhole"}
_MH_DIA_TOKENS = {"dia", "size", "mh dia", "chamber size", "internal", "diameter"}
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
) -> tuple[int | None, int | None, int | None, list[int]]:
    """Return (ref_col, dia_col, cover_col, invert_cols) indices."""
    ref_col: int | None = None
    dia_col: int | None = None
    cover_col: int | None = None
    invert_cols: list[int] = []

    for i, cell in enumerate(header_row):
        norm = _normalise_header(cell)
        # Check more-specific tokens first to avoid "mh" matching "mh dia (size)"
        if _header_matches(norm, _MH_DIA_TOKENS):
            dia_col = i
        elif _header_matches(norm, _COVER_TOKENS):
            cover_col = i
        elif _header_matches(norm, _INVERT_TOKENS):
            invert_cols.append(i)
        elif _header_matches(norm, _MH_REF_TOKENS):
            ref_col = i

    return ref_col, dia_col, cover_col, invert_cols


# ---------------------------------------------------------------------------
# Camelot-based extraction
# ---------------------------------------------------------------------------


def _extract_from_camelot_tables(
    tables: list[list[list[str]]],
    pdf_name: str,
    page_number: int,
) -> list[MHScheduleRow]:
    rows: list[MHScheduleRow] = []
    for table in tables:
        if len(table) < 2:
            continue
        # Find header row
        header_idx = 0
        for i, row in enumerate(table):
            joined = " ".join(row).lower()
            if "ref" in joined and ("invert" in joined or "cover" in joined):
                header_idx = i
                break

        header = table[header_idx]
        ref_col, dia_col, cover_col, invert_cols = _header_col_indices(header)

        if ref_col is None:
            log.debug(
                "pdf=%s page=%d camelot table has no MH REF column",
                pdf_name,
                page_number,
            )
            continue

        if not invert_cols:
            # Try to find invert columns heuristically in data rows
            for j in range(len(header)):
                if j not in (ref_col, dia_col, cover_col):
                    invert_cols.append(j)

        for row in table[header_idx + 1 :]:
            if len(row) <= ref_col:
                continue
            mh_ref = row[ref_col].strip()
            if not mh_ref or mh_ref.lower() in ("", "mh ref", "reference"):
                continue

            dia: int | None = None
            if dia_col is not None and dia_col < len(row):
                dia = _parse_dia(row[dia_col])

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
                    "pdf=%s page=%d row '%s': missing cover and inverts, skipping",
                    pdf_name,
                    page_number,
                    mh_ref,
                )
                continue

            invert = min(inv_values) if inv_values else None
            rows.append(
                MHScheduleRow(
                    mh_ref=mh_ref,
                    mh_dia=dia,
                    cover_level=cover,
                    invert_level=invert,
                )
            )

    return rows


# ---------------------------------------------------------------------------
# pdfplumber word-level fallback
# ---------------------------------------------------------------------------


def _cluster_by_x(words: list[dict], tolerance: float = 8.0) -> dict[float, list[dict]]:
    """Cluster words into columns by x0 position."""
    clusters: dict[float, list[dict]] = {}
    for w in sorted(words, key=lambda x: x["x0"]):
        x = w["x0"]
        placed = False
        for cx in list(clusters.keys()):
            if abs(x - cx) <= tolerance:
                clusters[cx].append(w)
                placed = True
                break
        if not placed:
            clusters[x] = [w]
    return clusters


def _cluster_by_y(words: list[dict], tolerance: float = 4.0) -> dict[float, list[dict]]:
    """Cluster words into lines by top position."""
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


def _is_mh_schedule_page(words: list[dict]) -> bool:
    """Return True if the page looks like a manhole schedule."""
    text = " ".join(w["text"].lower() for w in words)
    has_schedule = "schedule" in text or "manhole" in text
    has_ref = "ref" in text
    has_level = "invert" in text or "cover" in text
    return has_schedule and has_ref and has_level


def _merge_header_line(
    header_words: list[dict], merge_gap: float = 30.0
) -> list[tuple[float, str]]:
    """Merge adjacent words in a header line into cells.

    Returns list of (x0, joined_text) for each merged cell.
    """
    if not header_words:
        return []
    cells: list[tuple[float, str]] = []
    sorted_words = sorted(header_words, key=lambda w: w["x0"])
    current_x = sorted_words[0]["x0"]
    current_text = sorted_words[0]["text"]
    current_x1 = sorted_words[0]["x1"]

    for w in sorted_words[1:]:
        gap = w["x0"] - current_x1
        if gap <= merge_gap:
            # Adjacent word - merge into same cell
            current_text += " " + w["text"]
            current_x1 = w["x1"]
        else:
            cells.append((current_x, current_text))
            current_x = w["x0"]
            current_text = w["text"]
            current_x1 = w["x1"]
    cells.append((current_x, current_text))
    return cells


def _extract_from_words(
    words: list[dict],
    pdf_name: str,
    page_number: int,
) -> list[MHScheduleRow]:
    """Rebuild table from word-level data and extract MH rows."""
    if not words:
        return []

    # Group words into lines
    lines_dict = _cluster_by_y(words, tolerance=4.0)
    lines: list[list[dict]] = []
    for y_key in sorted(lines_dict.keys()):
        line_words = sorted(lines_dict[y_key], key=lambda w: w["x0"])
        lines.append(line_words)

    if not lines:
        return []

    # Find header line
    header_idx = 0
    for i, line in enumerate(lines):
        joined = " ".join(w["text"].lower() for w in line)
        if ("ref" in joined) and ("invert" in joined or "cover" in joined):
            header_idx = i
            break

    header_line = lines[header_idx]

    # Merge adjacent words in the header into cells, then detect column roles
    merged_cells = _merge_header_line(header_line, merge_gap=12.0)

    ref_col_x: float | None = None
    dia_col_x: float | None = None
    cover_col_x: float | None = None
    invert_col_xs: list[float] = []
    col_positions: list[float] = [cx for cx, _ in merged_cells]

    for x, label in merged_cells:
        norm = _normalise_header(label)
        if _header_matches(norm, _MH_DIA_TOKENS):
            dia_col_x = x
        elif _header_matches(norm, _COVER_TOKENS):
            cover_col_x = x
        elif _header_matches(norm, _INVERT_TOKENS):
            invert_col_xs.append(x)
        elif _header_matches(norm, _MH_REF_TOKENS):
            ref_col_x = x

    if ref_col_x is None:
        log.debug(
            "pdf=%s page=%d word fallback: no MH REF column found",
            pdf_name,
            page_number,
        )
        return []

    def snap_to_col(x: float, candidates: list[float], tol: float = 15.0) -> float | None:
        for cx in candidates:
            if abs(x - cx) <= tol:
                return cx
        return None

    rows: list[MHScheduleRow] = []
    for line in lines[header_idx + 1 :]:
        if not line:
            continue
        # Build a dict: col_x -> cell text
        cell: dict[float, str] = {}
        for w in line:
            cx = snap_to_col(w["x0"], col_positions, tol=20.0)
            if cx is not None:
                cell[cx] = cell.get(cx, "") + " " + w["text"]

        mh_ref = cell.get(ref_col_x, "").strip()
        if not mh_ref:
            continue

        # Skip header-like repeats
        if mh_ref.lower() in ("mh ref", "ref", "reference", "manhole ref"):
            continue

        dia: int | None = None
        if dia_col_x is not None:
            dia = _parse_dia(cell.get(dia_col_x, ""))

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
                "pdf=%s page=%d row '%s': missing cover and inverts, skipping",
                pdf_name,
                page_number,
                mh_ref,
            )
            continue

        invert = min(inv_values) if inv_values else None
        rows.append(
            MHScheduleRow(
                mh_ref=mh_ref,
                mh_dia=dia,
                cover_level=cover,
                invert_level=invert,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_mh_schedule(
    pdf_path: Path,
    use_ocr: bool = True,
) -> list[MHScheduleRow]:
    """Extract all MH schedule rows from a PDF file."""
    pdf_name = pdf_path.name
    all_rows: list[MHScheduleRow] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                log.info("pdf=%s page=%d extracting MH schedule", pdf_name, page_num)

                # Step 1: try camelot
                camelot_tables = try_camelot_stream_then_lattice(pdf_path, page_num)
                if camelot_tables:
                    rows = _extract_from_camelot_tables(camelot_tables, pdf_name, page_num)
                    if rows:
                        log.info(
                            "pdf=%s page=%d camelot found %d rows",
                            pdf_name,
                            page_num,
                            len(rows),
                        )
                        all_rows.extend(rows)
                        continue

                # Step 2: pdfplumber fallback
                words = get_page_words(page, pdf_path, page_num, use_ocr=use_ocr)
                if not _is_mh_schedule_page(words):
                    log.debug(
                        "pdf=%s page=%d does not look like MH schedule, skipping",
                        pdf_name,
                        page_num,
                    )
                    continue

                rows = _extract_from_words(words, pdf_name, page_num)
                if rows:
                    log.info(
                        "pdf=%s page=%d word fallback found %d rows",
                        pdf_name,
                        page_num,
                        len(rows),
                    )
                    all_rows.extend(rows)
                else:
                    log.warning(
                        "pdf=%s page=%d no MH rows extracted",
                        pdf_name,
                        page_num,
                    )
    except Exception as exc:
        log.error("pdf=%s failed to parse: %s", pdf_name, exc)

    return all_rows
