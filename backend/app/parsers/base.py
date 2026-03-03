from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ParseWarning:
    code: str
    detail: str


@dataclass
class ParsedCard:
    external_card_key: str
    section_index: int
    chapter_number: Optional[int]
    chapter_title: str
    question_number: int
    question_raw: str
    question_norm: str
    options: Dict[str, Dict[str, str]]
    answer_key: str
    answer_text_raw: str
    answer_text_norm: str
    explanation_raw: str
    explanation_norm: str
    warnings: List[ParseWarning] = field(default_factory=list)


@dataclass
class ParseResult:
    cards: List[ParsedCard]
    unresolved_anomalies: List[str]
    report: Dict[str, object]


class BaseParser:
    source_type: str

    def parse(self, file_path: Path) -> ParseResult:
        raise NotImplementedError
