# Manhole Schedule and Long Section Standardiser (`mhls`)

A UK roads and sewers tender helper that reads PDF manhole schedules and long
sections and outputs one Excel workbook in a fixed standard layout.

## Installation

```bash
pip install -e ".[dev]"
```

## Usage

```bash
mhls --input "C:/tender/pdf" --output "C:/tender/output/Takeoff_MH_and_LongSection_Levels.xlsx"
```

### CLI options

| Option | Required | Description |
|--------|----------|-------------|
| `--input` | Yes | Folder path containing PDF files |
| `--output` | Yes | Output `.xlsx` file path |
| `--log` | No | Log level (default: `INFO`) |
| `--no-ocr` | No | Disable OCR fallback for scanned pages |
| `--debug-dumps` | No | Write intermediate extraction data to a `dumps/` folder |

## Output

A single Excel file with these sheets:

- **MH_Schedule** – Manhole schedule rows (MH REF, MH DIA (size), Covel Level, Invert Level)
- **Lateral_MH_Schedule** – Lateral manhole rows (adds Diameter column)
- **LS_Rd1_Road1**, **LS_Rd2_Road2**, **LS_POS**, etc. – One sheet per long section table

## Project structure

```
src/mhls/
  __init__.py
  cli.py                      # CLI entry point
  pdf_read.py                 # Low-level PDF/OCR helpers
  extract_mh_schedule.py      # Manhole schedule extraction
  extract_lateral_mh_schedule.py  # Lateral schedule extraction
  extract_long_sections.py    # Long section band extraction
  excel_writer.py             # openpyxl workbook writing
  models.py                   # Dataclass row models
  logging_conf.py             # Logging setup
tests/
  fixtures/                   # Text-based test fixtures (no PDFs needed)
  test_mh_schedule.py
  test_lateral_mh_schedule.py
  test_long_sections.py
```

## Development

```bash
# Lint
ruff check src/ tests/
ruff format src/ tests/

# Test
pytest tests/ -v
```

## Long section parsing notes

### Band alignment method

UK long sections present data as **three horizontal bands** running left-to-right
across the page rather than as vertical table rows:

```
CHAINAGE            |  0.000  |  5.000  | 10.000 | ...
EXISTING GND LEVEL  | 99.123  | 98.456  | 97.789 | ...
ALIGNMENT LEVEL     | 98.000  | 97.500  | 97.000 | ...
```

The parser works in these steps:

1. **Find band labels** – scan all words on the page for tokens like `CHAINAGE`,
   `EXISTING`, `EGL`, `ALIGNMENT`, `DESIGN`, `PGL`.  The y-coordinate of the
   matching word defines the band's vertical centre.

2. **Collect numerics per band** – gather all words whose y-coordinate falls
   within ±8 points of the band centre and whose text matches a decimal number
   pattern (e.g. `0.000`, `99.500`).

3. **X-column clustering** – group numeric tokens by x-coordinate using a
   10-point tolerance.  This handles slight horizontal jitter between rows.

4. **Snap to chainage columns** – the chainage band establishes the master set
   of x-column positions.  Existing-ground and alignment values are snapped to
   the nearest chainage x-column within a 12-point tolerance.

5. **Build complete rows** – only output a row when all three values (chainage,
   existing, alignment) are present at the same column position.

6. **Continuation detection** – if the first chainage on page N+1 is ≤ the last
   chainage on page N, the parser treats it as a new table and creates a new
   sheet.

### Using debug dumps

Run with `--debug-dumps` to write a JSON file for every page to a `dumps/`
folder next to the output file.  Each file contains:

- `confidence` – score 0–1 for how good the extraction was
- `word_count` – number of words found on the page
- `rows_extracted` – number of complete rows produced
- `rows` – the actual chainage/existing/proposed values

If `confidence` is below 0.5, inspect the JSON to see which columns were
detected and which snaps failed.  Common causes of low confidence:
- The band labels use unusual abbreviations → add them to `_EXISTING_TOKENS` / `_ALIGNMENT_TOKENS`
- The x-column tolerance needs adjusting for a wider page layout
