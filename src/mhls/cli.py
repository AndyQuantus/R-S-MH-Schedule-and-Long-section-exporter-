"""Command-line interface for the Manhole Schedule and Long Section Standardiser.

Usage:
    mhls --input <folder> --output <xlsx_path> [--log LEVEL] [--no-ocr]
         [--debug-dumps]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from mhls.excel_writer import write_workbook
from mhls.extract_lateral_mh_schedule import extract_lateral_mh_schedule
from mhls.extract_long_sections import extract_long_sections
from mhls.extract_mh_schedule import extract_mh_schedule
from mhls.logging_conf import configure_logging
from mhls.models import LateralMHScheduleRow, LongSectionTable, MHScheduleRow

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PDF classification helpers
# ---------------------------------------------------------------------------


def _classify_pdf(pdf_path: Path) -> str:
    """Classify a PDF as 'mh_schedule', 'lateral_schedule', 'long_section', or 'unknown'."""
    name_lower = pdf_path.stem.lower()

    if "lateral" in name_lower:
        return "lateral_schedule"
    if "long section" in name_lower or "long_section" in name_lower or "longsection" in name_lower:
        return "long_section"
    if "manhole schedule" in name_lower or "mh schedule" in name_lower or "schedule" in name_lower:
        return "mh_schedule"

    # Fall back to content-based detection by trying all three and seeing what returns data
    return "unknown"


def _classify_by_content(pdf_path: Path, use_ocr: bool) -> str:
    """Attempt to classify unknown PDFs by partial content inspection."""
    try:
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[0] if pdf.pages else None
            if page is None:
                return "unknown"
            words = page.extract_words() or []
            text = " ".join(w.get("text", "").lower() for w in words)

            if "lateral" in text:
                return "lateral_schedule"
            if any(tok in text for tok in ("chainage", "alignment", "long section")):
                return "long_section"
            if any(tok in text for tok in ("manhole", "mh ref", "invert", "cover level")):
                return "mh_schedule"
    except Exception as exc:  # noqa: BLE001
        log.debug("Content classification failed for %s: %s", pdf_path.name, exc)
    return "unknown"


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------


def process_folder(
    input_dir: Path,
    output_path: Path,
    use_ocr: bool = True,
    debug_dumps: bool = False,
    dump_dir: Path | None = None,
) -> None:
    """Process all PDFs in input_dir and write output workbook."""
    pdf_files = sorted(input_dir.glob("**/*.pdf"))
    if not pdf_files:
        log.warning("No PDF files found in %s", input_dir)

    mh_rows: list[MHScheduleRow] = []
    lateral_rows: list[LateralMHScheduleRow] = []
    long_section_tables: list[LongSectionTable] = []

    for pdf_path in pdf_files:
        log.info("Processing: %s", pdf_path.name)
        classification = _classify_pdf(pdf_path)
        if classification == "unknown":
            classification = _classify_by_content(pdf_path, use_ocr)
            log.info("Content classification for %s: %s", pdf_path.name, classification)

        if classification == "mh_schedule":
            rows = extract_mh_schedule(pdf_path, use_ocr=use_ocr)
            log.info("  MH schedule rows extracted: %d", len(rows))
            mh_rows.extend(rows)

        elif classification == "lateral_schedule":
            rows = extract_lateral_mh_schedule(pdf_path, use_ocr=use_ocr)
            log.info("  Lateral MH schedule rows extracted: %d", len(rows))
            lateral_rows.extend(rows)

        elif classification == "long_section":
            tables = extract_long_sections(
                pdf_path,
                use_ocr=use_ocr,
                debug_dumps=debug_dumps,
                dump_dir=dump_dir,
            )
            log.info("  Long section tables extracted: %d", len(tables))
            for t in tables:
                log.info(
                    "    Sheet '%s': %d rows, confidence=%.2f",
                    t.sheet_name,
                    len(t.rows),
                    t.confidence,
                )
                if t.confidence < 0.5:
                    log.warning(
                        "    LOW CONFIDENCE for sheet '%s' in %s",
                        t.sheet_name,
                        pdf_path.name,
                    )
            long_section_tables.extend(tables)

        else:
            log.warning("Could not classify %s, skipping", pdf_path.name)

    # Ensure unique sheet names across all long section tables
    seen: dict[str, int] = {}
    for t in long_section_tables:
        if t.sheet_name in seen:
            seen[t.sheet_name] += 1
            t.sheet_name = f"{t.sheet_name}_{seen[t.sheet_name]}"[:31]
        else:
            seen[t.sheet_name] = 1

    write_workbook(output_path, mh_rows, lateral_rows, long_section_tables)

    # Summary
    ls_summary = {t.sheet_name: len(t.rows) for t in long_section_tables}
    print("\n=== Extraction Summary ===")
    print(f"Total MH rows:         {len(mh_rows)}")
    print(f"Total lateral MH rows: {len(lateral_rows)}")
    for sheet, count in ls_summary.items():
        print(f"Long section '{sheet}': {count} rows")
    low_conf = [t for t in long_section_tables if t.confidence < 0.5]
    if low_conf:
        print("\nLow confidence tables:")
        for t in low_conf:
            print(f"  '{t.sheet_name}' from {t.source_pdf} (confidence={t.confidence:.2f})")
    print(f"\nOutput written to: {output_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mhls",
        description="Manhole Schedule and Long Section Standardiser",
    )
    parser.add_argument(
        "--input",
        required=True,
        metavar="FOLDER",
        help="Input folder containing PDF files",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="XLSX",
        help="Output Excel file path (.xlsx)",
    )
    parser.add_argument(
        "--log",
        default="INFO",
        metavar="LEVEL",
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        default=False,
        help="Disable OCR fallback for pages without a text layer",
    )
    parser.add_argument(
        "--debug-dumps",
        action="store_true",
        default=False,
        help="Write intermediate extracted tables to a dumps folder",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(args.log)

    input_dir = Path(args.input)
    output_path = Path(args.output)

    if not input_dir.is_dir():
        log.error("Input folder does not exist: %s", input_dir)
        return 1

    dump_dir: Path | None = None
    if args.debug_dumps:
        dump_dir = output_path.parent / "dumps"

    process_folder(
        input_dir=input_dir,
        output_path=output_path,
        use_ocr=not args.no_ocr,
        debug_dumps=args.debug_dumps,
        dump_dir=dump_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
