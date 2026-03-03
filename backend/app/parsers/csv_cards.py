from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List

from .base import BaseParser, ParseResult, ParsedCard
from ..utils import normalize_text


class CsvCardParser(BaseParser):
    source_type = "csv"

    def parse(self, file_path: Path) -> ParseResult:
        cards: List[ParsedCard] = []
        unresolved: List[str] = []

        with file_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for idx, row in enumerate(reader, start=1):
                question_raw = (row.get("question") or row.get("question_raw") or "").strip()
                if not question_raw:
                    unresolved.append(f"Row {idx}: missing question text")
                    continue

                options: Dict[str, Dict[str, str]] = {}
                for key in ["A", "B", "C", "D", "E"]:
                    value = (row.get(f"option_{key.lower()}") or row.get(f"option_{key}") or "").strip()
                    if value:
                        options[key] = {"raw": value, "norm": normalize_text(value)}

                option_count = len(options)
                if option_count not in {2, 5}:
                    unresolved.append(f"Row {idx}: option count must be 2 or 5 (got {option_count})")
                    continue

                answer_key = (row.get("answer_key") or "").strip().upper()
                if answer_key not in options:
                    unresolved.append(f"Row {idx}: answer_key '{answer_key}' not found in options")
                    continue

                answer_text_raw = (row.get("answer_text") or options[answer_key]["raw"]).strip()
                explanation_raw = (row.get("explanation") or "").strip()
                chapter = (row.get("chapter") or "").strip() or "Imported CSV"
                question_number_raw = (row.get("question_number") or "").strip()
                try:
                    question_number = int(question_number_raw) if question_number_raw else idx
                except ValueError:
                    question_number = idx

                external_key = (row.get("external_card_key") or f"csv_q_{idx}").strip()
                cards.append(
                    ParsedCard(
                        external_card_key=external_key,
                        section_index=1,
                        chapter_number=None,
                        chapter_title=chapter,
                        question_number=question_number,
                        question_raw=question_raw,
                        question_norm=normalize_text(question_raw),
                        options=options,
                        answer_key=answer_key,
                        answer_text_raw=answer_text_raw,
                        answer_text_norm=normalize_text(answer_text_raw),
                        explanation_raw=explanation_raw,
                        explanation_norm=normalize_text(explanation_raw),
                    )
                )

        report = {
            "total_cards": len(cards),
            "unresolved_count": len(unresolved),
            "source": "csv",
        }
        return ParseResult(cards=cards, unresolved_anomalies=unresolved, report=report)
