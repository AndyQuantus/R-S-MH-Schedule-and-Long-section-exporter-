"""Extract long section chainage and level data from PDF files.

UK long sections present data as three horizontal bands (rows) running
left-to-right across the page:

  CHAINAGE            |  0.000  |  5.000  | 10.000 | ...
  EXISTING GND LEVEL  | 99.123  | 98.456  | 97.789 | ...
  ALIGNMENT LEVEL     | 98.000  | 97.500  | 97.000 | ...

The algorithm:
1. Find words labelling each band.
2. Collect numeric tokens in each band's y-strip.
3. Cluster numerics into x-columns.
4. Snap existing/alignment columns to chainage x-columns.
5. Build complete rows (all three values present).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from pathlib import Path

import pdfplumber

from mhls.models import LongSectionRow, LongSectionTable
from mhls.pdf_read import get_page_words

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / patterns
# ---------------------------------------------------------------------------

_CHAINAGE_TOKENS = {"chainage", "ch", "chain", "ch."}
_EXISTING_TOKENS = {
    "existing",
    "ground",
    "egl",
    "existing level",
    "existing ground",
    "ground level",
    "e.g.l",
}
_ALIGNMENT_TOKENS = {
    "alignment",
    "design",
    "proposed",
    "pgl",
    "alignment level",
    "design level",
    "proposed level",
    "p.g.l",
}

# Reject gradient/slope tokens near a numeric
_GRADIENT_RE = re.compile(r"(1\s*in|%|gradient|slope|fall)", re.IGNORECASE)
_DECIMAL_RE = re.compile(r"^-?\d{1,5}\.\d{1,4}$")


# ---------------------------------------------------------------------------
# Band label detection
# ---------------------------------------------------------------------------


def _is_chainage_label(text: str) -> bool:
    return text.lower().strip().rstrip(".") in _CHAINAGE_TOKENS


def _is_existing_label(text: str) -> bool:
    lower = text.lower().strip()
    return any(t in lower for t in _EXISTING_TOKENS)


def _is_alignment_label(text: str) -> bool:
    lower = text.lower().strip()
    return any(t in lower for t in _ALIGNMENT_TOKENS)


def _find_band_y(words: list[dict], label_fn) -> float | None:
    """Return the y-centre of the first word matching label_fn."""
    for w in words:
        if label_fn(w["text"]):
            return (w["top"] + w["bottom"]) / 2.0
    return None


# ---------------------------------------------------------------------------
# Numeric token filtering
# ---------------------------------------------------------------------------


def _looks_like_level(text: str) -> bool:
    """Return True if the text looks like a numeric level/chainage."""
    return bool(_DECIMAL_RE.match(text.strip()))


def _near_gradient(words: list[dict], w: dict, radius: float = 35.0) -> bool:
    """Reject numeric tokens that sit next to gradient or slope text."""
    wx0 = w["x0"]
    wy = (w["top"] + w["bottom"]) / 2.0
    for other in words:
        oy = (other["top"] + other["bottom"]) / 2.0
        if abs(oy - wy) > 10:
            continue
        if abs(other["x0"] - wx0) > radius:
            continue
        if _GRADIENT_RE.search(other.get("text", "")):
            return True
    return False


def _get_band_numerics(
    words: list[dict],
    band_y: float,
    band_half_height: float = 8.0,
) -> list[dict]:
    """Collect numeric words within the y band around band_y."""
    result: list[dict] = []
    for w in words:
        mid_y = (w["top"] + w["bottom"]) / 2.0
        if abs(mid_y - band_y) <= band_half_height and _looks_like_level(w["text"]):
            result.append(w)
    return result


# ---------------------------------------------------------------------------
# X-column clustering
# ---------------------------------------------------------------------------


def _cluster_x_positions(words: list[dict], tolerance: float = 10.0) -> dict[float, list[dict]]:
    """Cluster words into x columns by x0 position."""
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


def _pick_best_numeric(word_list: list[dict]) -> float | None:
    """From a cluster of candidate words, pick the best numeric value."""
    candidates: list[float] = []
    for w in word_list:
        with contextlib.suppress(ValueError):
            candidates.append(float(w["text"]))
    if not candidates:
        return None
    if len(candidates) > 1:
        log.debug("Multiple numerics in column cluster: %s, using first", candidates)
    return candidates[0]


# ---------------------------------------------------------------------------
# X-column snapping
# ---------------------------------------------------------------------------


def _snap_to_chainage_columns(
    src_clusters: dict[float, list[dict]],
    chainage_xs: list[float],
    tolerance: float = 12.0,
) -> dict[float, float | None]:
    """Map each chainage x-position to a numeric value from src_clusters."""
    result: dict[float, float | None] = {}
    for cx in chainage_xs:
        best_dist = tolerance + 1
        best_val: float | None = None
        for sx, words in src_clusters.items():
            dist = abs(sx - cx)
            if dist <= tolerance and dist < best_dist:
                val = _pick_best_numeric(words)
                if val is not None:
                    best_dist = dist
                    best_val = val
        result[cx] = best_val
    return result


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------


def _score_confidence(
    rows: list[LongSectionRow],
    found_chainage: bool,
    found_existing: bool,
    found_alignment: bool,
) -> float:
    score = 0.0
    if found_chainage:
        score += 0.3
    if found_existing:
        score += 0.2
    if found_alignment:
        score += 0.2
    n = len(rows)
    if n >= 10:
        score += 0.2
    elif n >= 5:
        score += 0.1

    if n >= 2:
        chainages = [r.chainage for r in rows]
        mono = all(chainages[i] < chainages[i + 1] for i in range(len(chainages) - 1))
        if mono:
            score += 0.1

    return min(score, 1.0)


# ---------------------------------------------------------------------------
# Column-based extraction helpers
# ---------------------------------------------------------------------------


def _is_chainage_value(v: float) -> bool:
    return 0.0 <= v <= 2000.0


def _is_level_value(v: float) -> bool:
    return 10.0 <= v <= 400.0


def _cluster_numeric_words_all(words: list[dict], x_tol: float = 20.0) -> dict[float, list[dict]]:
    nums = [w for w in words if _looks_like_level(w.get("text", "")) and not _near_gradient(words, w)]
    return _cluster_x_positions(nums, x_tol)


def _column_values_sorted_by_y(col_words: list[dict]) -> list[float]:
    col_words = sorted(col_words, key=lambda w: (w["top"], w["x0"]))
    vals: list[float] = []
    for w in col_words:
        with contextlib.suppress(ValueError):
            vals.append(float(w["text"]))
    return vals


def _pick_chainage_column(cols: dict[float, list[dict]]) -> tuple[float, list[float]] | None:
    best = None
    best_score = -1.0
    for x, ws in cols.items():
        vals = [v for v in _column_values_sorted_by_y(ws) if _is_chainage_value(v)]
        if len(vals) < 8:
            continue
        mono = sum(1 for i in range(len(vals) - 1) if vals[i + 1] >= vals[i]) / max(1, len(vals) - 1)
        mult5 = sum(1 for v in vals if abs((v / 5.0) - round(v / 5.0)) < 1e-6) / len(vals)
        starts_low = 1.0 if vals[0] <= 10.0 else 0.0
        score = (0.6 * mono) + (0.3 * mult5) + (0.1 * starts_low) + (0.02 * len(vals))
        if score > best_score:
            best_score = score
            best = (x, vals)
    return best


def _pick_level_columns(
    cols: dict[float, list[dict]], exclude_x: float
) -> list[tuple[float, list[float]]]:
    candidates: list[tuple[float, list[float]]] = []
    for x, ws in cols.items():
        if abs(x - exclude_x) < 1e-6:
            continue
        vals = [v for v in _column_values_sorted_by_y(ws) if _is_level_value(v)]
        if len(vals) >= 8:
            candidates.append((x, vals))
    candidates.sort(key=lambda t: len(t[1]), reverse=True)
    return candidates[:2]


def _extract_long_section_by_columns(words: list[dict]) -> list[LongSectionRow]:
    cols = _cluster_numeric_words_all(words, x_tol=20.0)
    if not cols:
        return []

    ch_pick = _pick_chainage_column(cols)
    if not ch_pick:
        return []

    ch_x, ch_vals = ch_pick
    lv_cols = _pick_level_columns(cols, exclude_x=ch_x)
    if len(lv_cols) < 2:
        return []

    lv_cols.sort(key=lambda t: t[0])
    ex_vals = lv_cols[0][1]
    pr_vals = lv_cols[1][1]

    n = min(len(ch_vals), len(ex_vals), len(pr_vals))
    if n < 8:
        return []

    rows: list[LongSectionRow] = []
    last = None
    for i in range(n):
        ch = ch_vals[i]
        ex = ex_vals[i]
        pr = pr_vals[i]
        if last is not None and ch < last:
            continue
        rows.append(LongSectionRow(chainage=ch, existing_level=ex, proposed_level=pr))
        last = ch
    return rows


# ---------------------------------------------------------------------------
# Sheet name derivation
# ---------------------------------------------------------------------------

_ROAD_LABEL_RE = re.compile(r"\b(Rd\s*\d+[A-Z]?|Road\s*\d+[A-Z]?|POS)\b", re.IGNORECASE)
_SAFE_CHARS_RE = re.compile(r"[^\w]")


def _derive_sheet_name(pdf_path: Path, page_words: list[dict] | None = None) -> str:
    """Derive a safe Excel sheet name from PDF filename or page content."""
    stem = pdf_path.stem

    # Try filename tokens like Rd1, Rd2, POS
    m = _ROAD_LABEL_RE.search(stem)
    if m:
        label = m.group(1).strip()
        # Normalise
        label_clean = re.sub(r"\s+", "", label)
        road_num_m = re.match(r"[Rr][Dd](\d+)([A-Z]?)", label_clean)
        if road_num_m:
            num = road_num_m.group(1)
            suffix = road_num_m.group(2)
            return f"LS_Rd{num}_Road{num}{suffix}"
        if label_clean.upper() == "POS":
            return "LS_POS"
        return f"LS_{label_clean}"[:31]

    # Try page content for road labels
    if page_words:
        text = " ".join(w["text"] for w in page_words)
        m2 = _ROAD_LABEL_RE.search(text)
        if m2:
            label = m2.group(1).strip()
            label_clean = re.sub(r"\s+", "", label)
            road_num_m = re.match(r"[Rr][Oo][Aa][Dd](\d+)([A-Z]?)", label_clean, re.I)
            if road_num_m:
                num = road_num_m.group(1)
                suffix = road_num_m.group(2)
                return f"LS_Rd{num}_Road{num}{suffix}"
            return f"LS_{label_clean}"[:31]

    # Fallback: use the PDF filename stem
    safe_stem = _SAFE_CHARS_RE.sub("_", stem)[:25]
    return f"LS_{safe_stem}"[:31]


# ---------------------------------------------------------------------------
# Per-page extraction
# ---------------------------------------------------------------------------


def _extract_page_rows(
    words: list[dict],
    pdf_name: str,
    page_num: int,
    band_half_height: float = 8.0,
    x_cluster_tol: float = 10.0,
    snap_tol: float = 12.0,
) -> tuple[list[LongSectionRow], float, bool, bool, bool]:
    """Extract long section rows from one page's words.

    Returns (rows, confidence, found_ch, found_ex, found_al).
    """
    ch_y = _find_band_y(words, _is_chainage_label)
    ex_y = _find_band_y(words, _is_existing_label)
    al_y = _find_band_y(words, _is_alignment_label)

    found_ch = ch_y is not None
    found_ex = ex_y is not None
    found_al = al_y is not None

    if not found_ch:
        log.debug("pdf=%s page=%d no chainage band found", pdf_name, page_num)
        return [], 0.0, False, False, False

    # Collect band numerics
    ch_words = _get_band_numerics(words, ch_y, band_half_height)  # type: ignore[arg-type]
    ch_clusters = _cluster_x_positions(ch_words, x_cluster_tol)

    if not ch_clusters:
        log.debug("pdf=%s page=%d no chainage numerics found", pdf_name, page_num)
        return [], 0.0, found_ch, found_ex, found_al

    # Build sorted chainage x positions and values
    ch_x_sorted = sorted(ch_clusters.keys())
    ch_values: dict[float, float | None] = {}
    for cx in ch_x_sorted:
        ch_values[cx] = _pick_best_numeric(ch_clusters[cx])

    rows: list[LongSectionRow] = []

    if found_ex and found_al:
        ex_words = _get_band_numerics(words, ex_y, band_half_height)  # type: ignore[arg-type]
        al_words = _get_band_numerics(words, al_y, band_half_height)  # type: ignore[arg-type]

        ex_clusters = _cluster_x_positions(ex_words, x_cluster_tol)
        al_clusters = _cluster_x_positions(al_words, x_cluster_tol)

        ex_snapped = _snap_to_chainage_columns(ex_clusters, ch_x_sorted, snap_tol)
        al_snapped = _snap_to_chainage_columns(al_clusters, ch_x_sorted, snap_tol)

        for cx in ch_x_sorted:
            ch_val = ch_values.get(cx)
            ex_val = ex_snapped.get(cx)
            al_val = al_snapped.get(cx)

            if ch_val is not None and ex_val is not None and al_val is not None:
                rows.append(
                    LongSectionRow(
                        chainage=ch_val,
                        existing_level=ex_val,
                        proposed_level=al_val,
                    )
                )
            else:
                log.debug(
                    "pdf=%s page=%d chainage x=%.1f: ch=%s ex=%s al=%s - incomplete",
                    pdf_name,
                    page_num,
                    cx,
                    ch_val,
                    ex_val,
                    al_val,
                )
    else:
        if not found_ex:
            log.warning("pdf=%s page=%d no existing ground band found", pdf_name, page_num)
        if not found_al:
            log.warning("pdf=%s page=%d no alignment band found", pdf_name, page_num)

    confidence = _score_confidence(rows, found_ch, found_ex, found_al)
    return rows, confidence, found_ch, found_ex, found_al


# ---------------------------------------------------------------------------
# Debug dumps
# ---------------------------------------------------------------------------


def _write_debug_dump(
    dump_dir: Path,
    pdf_name: str,
    page_num: int,
    words: list[dict],
    rows: list[LongSectionRow],
    confidence: float,
) -> None:
    """Write extracted words and rows to JSON for debugging."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(pdf_name).stem
    out = dump_dir / f"{stem}_page{page_num}.json"
    payload = {
        "pdf": pdf_name,
        "page": page_num,
        "confidence": confidence,
        "word_count": len(words),
        "rows_extracted": len(rows),
        "rows": [
            {"chainage": r.chainage, "existing": r.existing_level, "proposed": r.proposed_level}
            for r in rows
        ],
    }
    out.write_text(json.dumps(payload, indent=2))
    log.debug("Debug dump written: %s", out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_long_sections(
    pdf_path: Path,
    use_ocr: bool = True,
    debug_dumps: bool = False,
    dump_dir: Path | None = None,
) -> list[LongSectionTable]:
    """Extract all long section tables from a PDF file.

    Returns one LongSectionTable per detected table/road.
    """
    pdf_name = pdf_path.name
    tables: list[LongSectionTable] = []
    current_table: LongSectionTable | None = None
    last_chainage: float | None = None

    if debug_dumps and dump_dir is None:
        dump_dir = pdf_path.parent / "dumps"

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                log.info("pdf=%s page=%d extracting long section", pdf_name, page_num)

                words = get_page_words(page, pdf_path, page_num, use_ocr=use_ocr)

                rows, confidence, found_ch, found_ex, found_al = _extract_page_rows(
                    words, pdf_name, page_num
                )

                if debug_dumps and dump_dir is not None:
                    _write_debug_dump(dump_dir, pdf_name, page_num, words, rows, confidence)

                if not rows:
                    # Try the column-based fallback
                    rows = _extract_long_section_by_columns(words)
                    if rows:
                        log.warning(
                            "pdf=%s page=%d using column fallback, rows=%d",
                            pdf_name,
                            page_num,
                            len(rows),
                        )
                    else:
                        if found_ch:
                            log.warning(
                                "pdf=%s page=%d long section bands found but no complete rows",
                                pdf_name,
                                page_num,
                            )
                        continue

                # Detect if this is a continuation or a new table
                first_chainage = rows[0].chainage if rows else None

                if current_table is None:
                    # Start a new table
                    sheet_name = _derive_sheet_name(pdf_path, words)
                    current_table = LongSectionTable(
                        sheet_name=sheet_name,
                        source_pdf=pdf_name,
                    )
                    tables.append(current_table)
                elif (
                    last_chainage is not None
                    and first_chainage is not None
                    and first_chainage <= last_chainage
                ):
                    # New table — chainages reset
                    sheet_name = _derive_sheet_name(pdf_path, words)
                    # Make unique by appending table count
                    if len(tables) > 0:
                        sheet_name = f"{sheet_name}_{len(tables) + 1}"[:31]
                    current_table = LongSectionTable(
                        sheet_name=sheet_name,
                        source_pdf=pdf_name,
                    )
                    tables.append(current_table)

                current_table.rows.extend(rows)
                current_table.confidence = max(current_table.confidence, confidence)
                current_table.page_numbers.append(page_num)
                last_chainage = rows[-1].chainage if rows else last_chainage

                log.info(
                    "pdf=%s page=%d extracted %d rows, confidence=%.2f",
                    pdf_name,
                    page_num,
                    len(rows),
                    confidence,
                )

                if confidence < 0.5:
                    log.warning(
                        "pdf=%s page=%d low confidence %.2f for long section",
                        pdf_name,
                        page_num,
                        confidence,
                    )

    except Exception as exc:
        log.error("pdf=%s failed to parse long section: %s", pdf_name, exc)

    # Ensure unique sheet names across tables
    seen: dict[str, int] = {}
    for t in tables:
        if t.sheet_name in seen:
            seen[t.sheet_name] += 1
            t.sheet_name = f"{t.sheet_name}_{seen[t.sheet_name]}"[:31]
        else:
            seen[t.sheet_name] = 1

    return tables
