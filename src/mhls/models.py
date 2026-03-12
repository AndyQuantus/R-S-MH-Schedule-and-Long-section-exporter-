"""Data models for extracted schedule and long section rows."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MHScheduleRow:
    """A single row from a Manhole Schedule."""

    mh_ref: str
    mh_dia: int | None  # mm
    cover_level: float | None
    invert_level: float | None  # lowest invert


@dataclass
class LateralMHScheduleRow:
    """A single row from a Lateral Manhole Schedule."""

    mh_ref: str
    mh_dia: int | None  # size column value, mm
    cover_level: float | None
    invert_level: float | None  # lowest invert
    diameter: int | None  # explicit diameter column, mm


@dataclass
class LongSectionRow:
    """A single chainage entry from a long section table."""

    chainage: float
    existing_level: float
    proposed_level: float  # = alignment level


@dataclass
class LongSectionTable:
    """All rows from one long section table, plus metadata."""

    sheet_name: str
    rows: list[LongSectionRow] = field(default_factory=list)
    confidence: float = 0.0
    source_pdf: str = ""
    page_numbers: list[int] = field(default_factory=list)
