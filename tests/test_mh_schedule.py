"""Tests for MH schedule extraction."""

from __future__ import annotations

import pytest

from mhls.extract_mh_schedule import (
    _extract_from_camelot_tables,
    _extract_from_words,
    _parse_dia,
    _to_float,
)
from tests.fixtures import (
    MH_SCHEDULE_TABLE,
    MH_SCHEDULE_TABLE_MULTI_INVERT,
    MH_SCHEDULE_WORDS,
)


class TestToFloat:
    def test_basic_float(self) -> None:
        assert _to_float("50.250") == pytest.approx(50.25)

    def test_comma_decimal(self) -> None:
        assert _to_float("50,250") == pytest.approx(50.25)

    def test_invalid(self) -> None:
        assert _to_float("N/A") is None

    def test_empty(self) -> None:
        assert _to_float("") is None


class TestParseDia:
    def test_standard_sizes(self) -> None:
        assert _parse_dia("1200") == 1200
        assert _parse_dia("1500") == 1500
        assert _parse_dia("1800") == 1800

    def test_with_mm_suffix(self) -> None:
        assert _parse_dia("1200mm") == 1200

    def test_invalid(self) -> None:
        assert _parse_dia("N/A") is None

    def test_small_number_rejected(self) -> None:
        assert _parse_dia("50") is None


class TestExtractFromCamelotTables:
    def test_basic_extraction(self) -> None:
        rows = _extract_from_camelot_tables([MH_SCHEDULE_TABLE], "test.pdf", 1)
        assert len(rows) == 4
        refs = [r.mh_ref for r in rows]
        assert "S1" in refs
        assert "S4" in refs

    def test_correct_mh_dia(self) -> None:
        rows = _extract_from_camelot_tables([MH_SCHEDULE_TABLE], "test.pdf", 1)
        s1 = next(r for r in rows if r.mh_ref == "S1")
        assert s1.mh_dia == 1200

    def test_correct_cover_level(self) -> None:
        rows = _extract_from_camelot_tables([MH_SCHEDULE_TABLE], "test.pdf", 1)
        s1 = next(r for r in rows if r.mh_ref == "S1")
        assert s1.cover_level == pytest.approx(50.25)

    def test_lowest_invert_selected(self) -> None:
        """When multiple invert columns exist, the lowest must be selected."""
        rows = _extract_from_camelot_tables([MH_SCHEDULE_TABLE], "test.pdf", 1)
        s1 = next(r for r in rows if r.mh_ref == "S1")
        # Inverts were 47.100 and 47.050 → min = 47.050
        assert s1.invert_level == pytest.approx(47.05)

    def test_lowest_invert_multi_columns(self) -> None:
        """Multi-invert column table: minimum is selected."""
        rows = _extract_from_camelot_tables([MH_SCHEDULE_TABLE_MULTI_INVERT], "test.pdf", 1)
        f8 = next(r for r in rows if r.mh_ref == "F8")
        assert f8.invert_level == pytest.approx(52.1)

    def test_empty_invert_skipped(self) -> None:
        """Rows with empty inverts AND empty cover should be skipped."""
        bad_table = [
            ["MH REF", "MH DIA (size)", "Cover Level", "Invert Level"],
            ["X1", "1200", "", ""],
        ]
        rows = _extract_from_camelot_tables([bad_table], "test.pdf", 1)
        assert not rows

    def test_empty_table_returns_empty(self) -> None:
        rows = _extract_from_camelot_tables([[]], "test.pdf", 1)
        assert rows == []

    def test_invert_with_only_cover(self) -> None:
        """Row with cover but no invert should still be extracted."""
        table = [
            ["MH REF", "MH DIA (size)", "Cover Level", "Invert Level"],
            ["Y1", "1200", "50.000", ""],
        ]
        rows = _extract_from_camelot_tables([table], "test.pdf", 1)
        # Cover present, invert empty → should still create a row
        assert len(rows) == 1
        assert rows[0].invert_level is None


class TestExtractFromWords:
    def test_basic_word_extraction(self) -> None:
        rows = _extract_from_words(MH_SCHEDULE_WORDS, "test.pdf", 1)
        assert len(rows) == 2
        assert rows[0].mh_ref == "S1"
        assert rows[1].mh_ref == "S2"

    def test_cover_level_from_words(self) -> None:
        rows = _extract_from_words(MH_SCHEDULE_WORDS, "test.pdf", 1)
        s1 = rows[0]
        assert s1.cover_level == pytest.approx(50.25)

    def test_invert_level_from_words(self) -> None:
        rows = _extract_from_words(MH_SCHEDULE_WORDS, "test.pdf", 1)
        s1 = rows[0]
        assert s1.invert_level == pytest.approx(47.1)

    def test_mh_dia_from_words(self) -> None:
        rows = _extract_from_words(MH_SCHEDULE_WORDS, "test.pdf", 1)
        s1 = rows[0]
        assert s1.mh_dia == 1200

    def test_empty_words_returns_empty(self) -> None:
        rows = _extract_from_words([], "test.pdf", 1)
        assert rows == []
