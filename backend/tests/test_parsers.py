from __future__ import annotations

from pathlib import Path

import pytest

from app.parsers.aabb_pdf import AABBPdfParser
from app.parsers.csv_cards import CsvCardParser


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = PROJECT_ROOT / "AABB Self Assessment - Copy.pdf"


def test_aabb_parser_regression_on_real_pdf():
    if not PDF_PATH.exists():
        pytest.skip(f"Missing fixture PDF: {PDF_PATH}")

    parser = AABBPdfParser()
    result = parser.parse(PDF_PATH)

    assert result.unresolved_anomalies == []
    assert len(result.cards) == 915

    by_key = {c.external_card_key: c for c in result.cards}
    assert by_key["s12_q20"].answer_key == "C"
    assert by_key["s12_q48"].answer_key == "A"
    assert by_key["s15_q38"].question_number == 38

    two_option_count = sum(1 for c in result.cards if len(c.options) == 2)
    assert two_option_count == 2


def test_csv_parser_valid(tmp_path: Path):
    csv_path = tmp_path / "cards.csv"
    csv_path.write_text(
        "question,option_a,option_b,option_c,option_d,option_e,answer_key,answer_text,explanation,chapter,question_number\n"
        "What is 2+2?,3,4,5,6,7,B,4,Basic arithmetic,Math,1\n",
        encoding="utf-8",
    )

    parser = CsvCardParser()
    result = parser.parse(csv_path)

    assert result.unresolved_anomalies == []
    assert len(result.cards) == 1
    assert result.cards[0].answer_key == "B"


def test_csv_parser_rejects_bad_option_count(tmp_path: Path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text(
        "question,option_a,option_b,answer_key\n"
        "Question?,Yes,,A\n",
        encoding="utf-8",
    )

    parser = CsvCardParser()
    result = parser.parse(csv_path)

    assert len(result.cards) == 0
    assert result.unresolved_anomalies
