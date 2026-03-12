"""Extract long section chainage and level data from PDF files.

UK long sections often present data as three horizontal bands running left to right.

CHAINAGE            0.000  5.000  10.000 ...
EXISTING LEVELS     99.123 98.456 97.789 ...
PROPOSED LEVELS     98.000 97.500 97.000 ...

This module extracts those three bands and outputs complete rows only.

Main improvements in this version
1) Wider x clustering and snap tolerances for real world drawings
2) Fallback that aligns by left to right index order when snapping produces zero complete rows
3) Better debug dumps so you can see what was found on each page
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Callable, Iterable

import pdfplumber

from mhls.models import LongSectionRow, LongSectionTable
from mhls.pdf_read import get_page_words

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tokens and patterns
# ---------------------------------------------------------------------------

_CHAINAGE_TOKENS = {"chainage", "ch", "chain", "ch.", "sta", "station"}

_EXISTING_TOKENS = {
    "existing",
    "egl",
    "existing level",
    "existing levels",
    "existing ground level",
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
    "proposed levels",
    "p.g.l",
}

_DECIMAL_RE = re.compile(r"^-?\d{1,5}\.\d{1,4}$", re.IGNORECASE)
_GRADIENT_RE = re.compile(r"(1\s*in|%|gradient|slope|fall)", re.IGNORECASE)

# Sensible defaults for long sections
DEFAULT_BAND_HALF_HEIGHT = 10.0
DEFAULT_X_CLUSTER_TOL = 16.0
DEFAULT_SNAP_TOL = 28.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_chainage_label(text: str) -> bool:
    return _norm(text).rstrip(".") in _CHAINAGE_TOKENS

def _is_existing_label(text: str) -> bool:
    lower = _norm(text)
    return any(t in lower for t in _EXISTING_TOKENS)

def _is_alignment_label(text: str) -> bool:
    lower = _norm(text)
    return any(t in lower for t in _ALIGNMENT_TOKENS)


def _find_band_y(words: list[dict], label_fn) -> float | None:
    """Return the y-centre of the first word matching label_fn."""
    for w in words:
        if label_fn(w.get("text", "")):
            return (w["top"] + w["bottom"]) / 2.0
    return None


# ---------------------------------------------------------------------------
# Numeric token filtering
# ---------------------------------------------------------------------------


def _looks_like_level(text: str) -> bool:
    """Return True if the text looks like a numeric level/chainage."""
    return bool(_DECIMAL_RE.match(text.strip()))


def _get_band_numerics(
    words: list[dict],
    band_y: float,
    band_half_height: float,
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
        x = float(w["x0"])
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
    src_values: dict[float, float | None],
    chainage_xs: list[float],
    tolerance: float,
) -> dict[float, float | None]:
    """Map each chainage x-position to a numeric value from src_values by nearest x."""
    result: dict[float, float | None] = {}
    for cx in chainage_xs:
        best_dist = tolerance + 1.0
        best_val: float | None = None
        for sx, val in src_values.items():
            if val is None:
                continue
            dist = abs(sx - cx)
            if dist <= tolerance and dist < best_dist:
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
    ch_cols: int,
    ex_cols: int,
    al_cols: int,
) -> float:
    score = 0.0
    if found_chainage:
        score += 0.25
    if found_existing:
        score += 0.2
    if found_alignment:
        score += 0.2

    if ch_cols >= 10:
        score += 0.15
    if ex_cols >= 10:
        score += 0.1
    if al_cols >= 10:
        score += 0.1

    n = len(rows)
    if n >= 10:
        score += 0.1
    elif n >= 5:
        score += 0.05

    if n >= 2:
        chainages = [r.chainage for r in rows]
        mono = all(
            chainages[i] < chainages[i + 1]
            for i in range(len(chainages) - 1)
        )
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


def _cluster_numeric_words_all(
    words: list[dict], x_tol: float = 20.0
) -> dict[float, list[dict]]:
    nums = [
        w
        for w in words
        if _looks_like_level(w.get("text", ""))
        and not _near_gradient(words, w)
    ]
    return _cluster_x_positions(nums, x_tol)


def _column_values_sorted_by_y(col_words: list[dict]) -> list[float]:
    sorted_words = sorted(col_words, key=lambda w: (w["top"], w["x0"]))
    vals: list[float] = []
    for w in sorted_words:
        with contextlib.suppress(ValueError):
            vals.append(float(w["text"]))
    return vals


def _pick_chainage_column(
    cols: dict[float, list[dict]]
) -> tuple[float, list[float]] | None:
    best = None
    best_score = -1.0
    for x, ws in cols.items():
        vals = [
            v
            for v in _column_values_sorted_by_y(ws)
            if _is_chainage_value(v)
        ]
        if len(vals) < 8:
            continue
        mono = (
            sum(
                1
                for i in range(len(vals) - 1)
                if vals[i + 1] >= vals[i]
            )
            / max(1, len(vals) - 1)
        )
        mult5 = (
            sum(
                1
                for v in vals
                if abs((v / 5.0) - round(v / 5.0)) < 1e-6
            )
            / len(vals)
        )
        starts_low = 1.0 if vals[0] <= 10.0 else 0.0
        score = (
            (0.6 * mono)
            + (0.3 * mult5)
            + (0.1 * starts_low)
            + (0.02 * len(vals))
        )
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
        vals = [
            v
            for v in _column_values_sorted_by_y(ws)
            if _is_level_value(v)
        ]
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
        rows.append(
            LongSectionRow(
                chainage=ch,
                existing_level=ex,
                proposed_level=pr
            )
        )
        last = ch
    return rows


# ---------------------------------------------------------------------------
# Sheet name derivation
# ---------------------------------------------------------------------------

_ROAD_LABEL_RE = re.compile(
    r"\b(Rd\s*\d+[A-Z]?|Road\s*\d+[A-Z]?|POS)\b", re.IGNORECASE
)
_SAFE_CHARS_RE = re.compile(r"[^\w]")


def _derive_sheet_name(pdf_path: Path, page_words: list[dict] | None = None) -> str:
    """Derive a safe Excel sheet name from PDF filename or page content."""
    stem = pdf_path.stem

    m = _ROAD_LABEL_RE.search(stem)
    if m:
        label = m.group(1).strip()
        label_clean = re.sub(r"\s+", "", label)
        rd = re.match(r"[Rr][Dd](\d+)([A-Z]?)", label_clean)
        if rd:
            num = rd.group(1)
            suffix = rd.group(2)
            return f"LS_Rd{num}_Road{num}{suffix}"[:31]
        if label_clean.upper() == "POS":
            return "LS_POS"
        return f"LS_{label_clean}"[:31]

    if page_words:
        text = " ".join(w.get("text", "") for w in page_words)
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

    safe_stem = _SAFE_CHARS_RE.sub("_", stem)[:25]
    return f"LS_{safe_stem}"[:31]


# ---------------------------------------------------------------------------
# Per-page extraction
# ---------------------------------------------------------------------------

def _extract_page_rows(
    words: list[dict],
    pdf_name: str,
    page_num: int,
    band_half_height: float = DEFAULT_BAND_HALF_HEIGHT,
    x_cluster_tol: float = DEFAULT_X_CLUSTER_TOL,
    snap_tol: float = DEFAULT_SNAP_TOL,
) -> tuple[
    list[LongSectionRow],
    float,
    dict,
]:
    """
    Returns rows, confidence, and debug details dict.
    """
    ch_y = _find_band_y(words, _is_chainage_label)
    ex_y = _find_band_y(words, _is_existing_label)
    al_y = _find_band_y(words, _is_alignment_label)

    found_ch = ch_y is not None
    found_ex = ex_y is not None
    found_al = al_y is not None

    debug: dict = {
        "pdf": pdf_name,
        "page": page_num,
        "found_chainage_band": found_ch,
        "found_existing_band": found_ex,
        "found_alignment_band": found_al,
        "band_y": {"chainage": ch_y, "existing": ex_y, "alignment": al_y},
        "tolerances": {
            "band_half_height": band_half_height,
            "x_cluster_tol": x_cluster_tol,
            "snap_tol": snap_tol,
        },
    }

    if not found_ch:
        log.debug("pdf=%s page=%d no chainage band found", pdf_name, page_num)
        return [], 0.0, False, False, False

    # Collect band numerics
    ch_words = _get_band_numerics(words, ch_y, band_half_height)  # type: ignore[arg-type]
    ch_clusters = _cluster_x_positions(ch_words, x_cluster_tol)
    ch_vals_by_x = _clusters_to_values(ch_clusters)
    ch_xs = sorted(ch_clusters.keys())

    debug["band_counts"] = {"chainage": len(ch_words), "existing": 0, "alignment": 0}
    debug["column_counts"] = {"chainage": len(ch_clusters), "existing": 0, "alignment": 0}
    debug["chainage_columns_x"] = [round(x, 2) for x in ch_xs]

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
        ex_words = _get_band_numerics(
            words, ex_y, band_half_height
        )  # type: ignore[arg-type]
        al_words = _get_band_numerics(
            words, al_y, band_half_height
        )  # type: ignore[arg-type]

        ex_clusters = _cluster_x_positions(ex_words, x_cluster_tol)
        al_clusters = _cluster_x_positions(al_words, x_cluster_tol)

        ex_snapped = _snap_to_chainage_columns(ex_clusters, ch_x_sorted, snap_tol)
        al_snapped = _snap_to_chainage_columns(al_clusters, ch_x_sorted, snap_tol)

        debug["band_counts"]["existing"] = len(ex_words)
        debug["band_counts"]["alignment"] = len(al_words)
        debug["column_counts"]["existing"] = len(ex_clusters)
        debug["column_counts"]["alignment"] = len(al_clusters)

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
                    len(fallback_rows),
                )
                rows = fallback_rows
                debug["used_fallback"] = "index_order"
            else:
                debug["used_fallback"] = None
        else:
            debug["used_fallback"] = None

    else:
        if not found_ex:
            log.warning("pdf=%s page=%d no existing ground band found", pdf_name, page_num)
        if not found_al:
            log.warning("pdf=%s page=%d no alignment band found", pdf_name, page_num)

    confidence = _score_confidence(
        rows=rows,
        found_chainage=found_ch,
        found_existing=found_ex,
        found_alignment=found_al,
        ch_cols=len(ch_clusters),
        ex_cols=debug["column_counts"]["existing"],
        al_cols=debug["column_counts"]["alignment"],
    )
    debug["confidence"] = confidence
    debug["rows_extracted"] = len(rows)

    return rows, confidence, debug


# ---------------------------------------------------------------------------
# Debug dumps
# ---------------------------------------------------------------------------

def _write_debug_dump(
    dump_dir: Path,
    pdf_name: str,
    page_num: int,
    words: list[dict],
    rows: list[LongSectionRow],
    debug: dict,
) -> None:
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
    pdf_name = pdf_path.name
    tables: list[LongSectionTable] = []
    current_table: LongSectionTable | None = None
    last_chainage: float | None = None

    if debug_dumps and dump_dir is None:
        dump_dir = pdf_path.parent / "dumps"

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                log.info(
                    "pdf=%s page=%d extracting long section",
                    pdf_name,
                    page_num,
                )

                words = get_page_words(
                    page, pdf_path, page_num, use_ocr=use_ocr
                )

                rows, confidence, found_ch, found_ex, found_al = _extract_page_rows(
                    words, pdf_name, page_num
                )

                if debug_dumps and dump_dir is not None:
                    _write_debug_dump(dump_dir, pdf_name, page_num, words, rows, confidence)

                if not rows:
                    if found_ch:
                        log.warning(
                            "pdf=%s page=%d using column fallback, rows=%d",
                            pdf_name,
                            page_num,
                            len(rows),
                        )
                    else:
                        if found_ch:
                            log.warning(
                                "pdf=%s page=%d long section bands found but"
                                " no complete rows",
                                pdf_name,
                                page_num,
                            )
                        continue

                first_chainage = rows[0].chainage

                if current_table is None:
                    sheet_name = _derive_sheet_name(pdf_path, words)
                    current_table = LongSectionTable(sheet_name=sheet_name, source_pdf=pdf_name)
                    tables.append(current_table)
                elif last_chainage is not None and first_chainage <= last_chainage:
                    sheet_name = _derive_sheet_name(pdf_path, words)
                    sheet_name = f"{sheet_name}_{len(tables) + 1}"[:31]
                    current_table = LongSectionTable(sheet_name=sheet_name, source_pdf=pdf_name)
                    tables.append(current_table)

                current_table.rows.extend(rows)
                current_table.confidence = max(
                    current_table.confidence, confidence
                )
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
                        "pdf=%s page=%d low confidence %.2f for long"
                        " section",
                        pdf_name,
                        page_num,
                        confidence,
                    )

    except Exception as exc:
        log.error("pdf=%s failed to parse long section: %s", pdf_name, exc)

    seen: dict[str, int] = {}
    for t in tables:
        if t.sheet_name in seen:
            seen[t.sheet_name] += 1
            t.sheet_name = f"{t.sheet_name}_{seen[t.sheet_name]}"[:31]
        else:
            seen[t.sheet_name] = 1

    return tables

