"""Test fixtures for mhls tests.

These are plain Python data structures that simulate the output of pdfplumber
word extraction and camelot table extraction, so no large PDFs are needed.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# MH Schedule fixtures
# ---------------------------------------------------------------------------


def make_word(text: str, x0: float, x1: float, top: float, bottom: float) -> dict:
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


# A simple manhole schedule table (camelot-like list of rows)
MH_SCHEDULE_TABLE: list[list[str]] = [
    ["MH REF", "MH DIA (size)", "Cover Level", "Invert Level 1", "Invert Level 2"],
    ["S1", "1200", "50.250", "47.100", "47.050"],
    ["S2", "1200", "50.100", "46.900", ""],
    ["S3", "1500", "49.800", "46.500", "46.480"],
    ["S4", "1200", "49.500", "46.200", ""],
]

# Table where invert columns have multiple values – pick the minimum
MH_SCHEDULE_TABLE_MULTI_INVERT: list[list[str]] = [
    ["MH REF", "MH DIA (size)", "Cover Level", "Invert 1", "Invert 2", "Invert 3"],
    ["F8", "1800", "55.000", "52.500", "52.300", "52.100"],  # min = 52.100
    ["F22", "1800", "54.500", "51.800", "51.750", ""],  # min = 51.750
]

# Word-level representation of a simple MH schedule page
MH_SCHEDULE_WORDS: list[dict] = [
    # Header row at y=10
    make_word("MH", 10, 30, 10, 18),
    make_word("REF", 30, 55, 10, 18),
    make_word("MH", 70, 90, 10, 18),
    make_word("DIA", 90, 115, 10, 18),
    make_word("Cover", 150, 185, 10, 18),
    make_word("Level", 185, 215, 10, 18),
    make_word("Invert", 250, 285, 10, 18),
    make_word("Level", 285, 315, 10, 18),
    # Data row 1 at y=25
    make_word("S1", 10, 40, 25, 33),
    make_word("1200", 70, 110, 25, 33),
    make_word("50.250", 150, 200, 25, 33),
    make_word("47.100", 250, 300, 25, 33),
    # Data row 2 at y=40
    make_word("S2", 10, 40, 40, 48),
    make_word("1200", 70, 110, 40, 48),
    make_word("50.100", 150, 200, 40, 48),
    make_word("46.900", 250, 300, 40, 48),
    # Extra keyword to make page detection pass
    make_word("Manhole", 10, 60, 2, 8),
    make_word("Schedule", 60, 120, 2, 8),
]


# ---------------------------------------------------------------------------
# Lateral MH Schedule fixtures
# ---------------------------------------------------------------------------

# Lateral table with both 'MH DIA (size)' and 'Diameter' columns
LATERAL_TABLE_WITH_BOTH: list[list[str]] = [
    ["MH REF", "MH DIA (size)", "Cover Level", "Invert Level", "Diameter"],
    ["PS1", "1200", "50.000", "47.000", "1050"],
    ["PF1", "", "49.500", "46.800", "450"],
    ["L1", "1200", "49.000", "46.500", "350"],
]

# Lateral table with only 'Diameter' column (no size)
LATERAL_TABLE_DIAMETER_ONLY: list[list[str]] = [
    ["MH REF", "Cover Level", "Invert Level", "Diameter"],
    ["L2", "48.500", "45.000", "450"],
    ["L3", "48.000", "44.500", "350"],
]

# Lateral table with only size column (no explicit diameter)
LATERAL_TABLE_SIZE_ONLY: list[list[str]] = [
    ["MH REF", "MH DIA (size)", "Cover Level", "Invert Level"],
    ["L4", "1200", "48.000", "44.200"],
]


# ---------------------------------------------------------------------------
# Long section fixtures
# ---------------------------------------------------------------------------

# Simulated word-level representation of a long section page with three bands
# Band y positions: chainage=50, existing=100, alignment=150
# Chainage columns at x: 100, 200, 300, 400, 500
# Chainage values:        0.000, 5.000, 10.000, 15.000, 20.000
# Existing values:       99.500, 99.200, 98.900, 98.600, 98.300
# Alignment values:      98.000, 97.500, 97.000, 96.500, 96.000

LONG_SECTION_WORDS: list[dict] = [
    # Chainage band label at y=50
    make_word("CHAINAGE", 10, 80, 46, 54),
    # Chainage values
    make_word("0.000", 100, 130, 46, 54),
    make_word("5.000", 200, 230, 46, 54),
    make_word("10.000", 300, 335, 46, 54),
    make_word("15.000", 400, 435, 46, 54),
    make_word("20.000", 500, 535, 46, 54),
    # Existing ground band label at y=100
    make_word("EXISTING", 10, 70, 96, 104),
    make_word("GROUND", 70, 125, 96, 104),
    make_word("LEVEL", 125, 160, 96, 104),
    # Existing values
    make_word("99.500", 100, 135, 96, 104),
    make_word("99.200", 200, 235, 96, 104),
    make_word("98.900", 300, 335, 96, 104),
    make_word("98.600", 400, 435, 96, 104),
    make_word("98.300", 500, 535, 96, 104),
    # Alignment band label at y=150
    make_word("ALIGNMENT", 10, 80, 146, 154),
    make_word("LEVEL", 80, 115, 146, 154),
    # Alignment values
    make_word("98.000", 100, 135, 146, 154),
    make_word("97.500", 200, 235, 146, 154),
    make_word("97.000", 300, 335, 146, 154),
    make_word("96.500", 400, 435, 146, 154),
    make_word("96.000", 500, 535, 146, 154),
]

# Expected extracted rows from LONG_SECTION_WORDS
LONG_SECTION_EXPECTED = [
    (0.0, 99.5, 98.0),
    (5.0, 99.2, 97.5),
    (10.0, 98.9, 97.0),
    (15.0, 98.6, 96.5),
    (20.0, 98.3, 96.0),
]
