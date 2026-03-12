"""Tests for lateral MH schedule extraction."""

from __future__ import annotations

import pytest

from mhls.extract_lateral_mh_schedule import _from_camelot, _parse_mm, _to_float
from tests.fixtures import (
    LATERAL_TABLE_DIAMETER_ONLY,
    LATERAL_TABLE_SIZE_ONLY,
    LATERAL_TABLE_WITH_BOTH,
)


class TestToFloat:
    def test_basic(self) -> None:
        assert _to_float("49.500") == pytest.approx(49.5)

    def test_empty(self) -> None:
        assert _to_float("") is None


class TestParseMm:
    def test_integer_mm(self) -> None:
        assert _parse_mm("1050") == 1050
        assert _parse_mm("350") == 350
        assert _parse_mm("450") == 450

    def test_with_text(self) -> None:
        assert _parse_mm("450mm IC") == 450

    def test_invalid(self) -> None:
        assert _parse_mm("") is None
        assert _parse_mm("N/A") is None


class TestLateralFromCamelot:
    def test_both_dia_and_diameter_columns(self) -> None:
        """MH DIA (size) and Diameter must be written to separate columns."""
        rows = _from_camelot([LATERAL_TABLE_WITH_BOTH], "test.pdf", 1)
        assert len(rows) == 3

        ps1 = next(r for r in rows if r.mh_ref == "PS1")
        # MH DIA (size) from 'size' column = 1200
        assert ps1.mh_dia == 1200
        # Diameter from explicit 'diameter' column = 1050
        assert ps1.diameter == 1050

    def test_diameter_not_copied_to_mh_dia(self) -> None:
        """When only Diameter column exists, mh_dia must remain None."""
        rows = _from_camelot([LATERAL_TABLE_DIAMETER_ONLY], "test.pdf", 1)
        assert len(rows) == 2
        l2 = rows[0]
        assert l2.mh_dia is None  # no size column
        assert l2.diameter == 450

    def test_mh_dia_not_copied_to_diameter(self) -> None:
        """When only size column exists, diameter must remain None."""
        rows = _from_camelot([LATERAL_TABLE_SIZE_ONLY], "test.pdf", 1)
        assert len(rows) == 1
        l4 = rows[0]
        assert l4.mh_dia == 1200
        assert l4.diameter is None  # no diameter column

    def test_ps_pf_refs_preserved(self) -> None:
        """PS and PF references must be kept exactly as written."""
        rows = _from_camelot([LATERAL_TABLE_WITH_BOTH], "test.pdf", 1)
        refs = [r.mh_ref for r in rows]
        assert "PS1" in refs
        assert "PF1" in refs

    def test_empty_mh_dia_stays_none(self) -> None:
        """Empty size field should result in None mh_dia."""
        rows = _from_camelot([LATERAL_TABLE_WITH_BOTH], "test.pdf", 1)
        pf1 = next(r for r in rows if r.mh_ref == "PF1")
        assert pf1.mh_dia is None

    def test_lowest_invert_selected(self) -> None:
        """Multiple inverts → lowest selected."""
        table = [
            ["MH REF", "Cover Level", "Invert Level", "Invert Level 2"],
            ["L10", "50.000", "47.500", "47.200"],
        ]
        rows = _from_camelot([table], "test.pdf", 1)
        assert len(rows) == 1
        assert rows[0].invert_level == pytest.approx(47.2)

    def test_cover_and_invert_required(self) -> None:
        """Rows with no cover and no inverts should be skipped."""
        table = [
            ["MH REF", "Cover Level", "Invert Level"],
            ["BAD", "", ""],
        ]
        rows = _from_camelot([table], "test.pdf", 1)
        assert not rows
