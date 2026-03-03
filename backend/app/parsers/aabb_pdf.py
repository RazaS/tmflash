from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .base import BaseParser, ParseResult, ParseWarning, ParsedCard
from ..utils import normalize_text


QUESTION_RE = re.compile(r"^\s*Question\s+(\d+)\s*:?\s*(.*)$", re.IGNORECASE)
ANSWER_STD_RE = re.compile(r"^\s*Question\s+(\d+)\s*:\s*([A-E])\.\s*(.*)$", re.IGNORECASE)
ANSWER_OPEN_RE = re.compile(r"^\s*Question\s+(\d+)\s*:?\s*$", re.IGNORECASE)
OPTION_RE = re.compile(r"^\s*([A-E])\s*\.\s*(.*)$")


class AABBPdfParser(BaseParser):
    source_type = "pdf"

    def parse(self, file_path: Path) -> ParseResult:
        text = self._extract_text(file_path)
        lines = [line.replace("\r", "") for line in text.splitlines()]
        sections = self._parse_sections(lines)

        cards: List[ParsedCard] = []
        unresolved: List[str] = []
        section_counts: Dict[int, int] = {}

        for section in sections:
            title = section["title"]
            expected_count = 15 if "bonus" in title.lower() else 50
            question_map: Dict[int, Dict] = section["questions"]
            answer_map: Dict[int, Dict] = section["answers"]
            section_counts[section["index"]] = len(question_map)

            if len(question_map) != expected_count:
                unresolved.append(
                    f"Section {section['index']} '{title}' has {len(question_map)} questions; expected {expected_count}."
                )
            if len(answer_map) != expected_count:
                unresolved.append(
                    f"Section {section['index']} '{title}' has {len(answer_map)} answers; expected {expected_count}."
                )

            for qnum in range(1, expected_count + 1):
                if qnum not in question_map:
                    unresolved.append(f"Section {section['index']} missing question {qnum}.")
                if qnum not in answer_map:
                    unresolved.append(f"Section {section['index']} missing answer {qnum}.")

            for qnum in sorted(question_map):
                q = question_map[qnum]
                a = answer_map.get(qnum)
                if a is None:
                    continue

                options = q["options"]
                option_keys = sorted(options.keys())
                if len(option_keys) not in {2, 5}:
                    unresolved.append(
                        f"Section {section['index']} question {qnum} has invalid option count {len(option_keys)}."
                    )

                answer_key = a.get("answer_key")
                if not answer_key:
                    unresolved.append(f"Section {section['index']} question {qnum} has no resolved answer key.")
                    continue
                if answer_key not in options:
                    unresolved.append(
                        f"Section {section['index']} question {qnum} answer key '{answer_key}' missing in options."
                    )
                    continue

                answer_text_raw = "\n".join([x for x in a["answer_text_lines"] if x]).strip()
                if not answer_text_raw:
                    answer_text_raw = options[answer_key]["raw"]

                explanation_raw = "\n".join([x for x in a["explanation_lines"] if x]).strip()

                warnings = q["warnings"] + a["warnings"]

                cards.append(
                    ParsedCard(
                        external_card_key=f"s{section['index']}_q{qnum}",
                        section_index=section["index"],
                        chapter_number=section["chapter_number"],
                        chapter_title=title,
                        question_number=qnum,
                        question_raw=q["question_raw"],
                        question_norm=normalize_text(q["question_raw"]),
                        options=options,
                        answer_key=answer_key,
                        answer_text_raw=answer_text_raw,
                        answer_text_norm=normalize_text(answer_text_raw),
                        explanation_raw=explanation_raw,
                        explanation_norm=normalize_text(explanation_raw),
                        warnings=warnings,
                    )
                )

        report = {
            "source": "aabb_pdf",
            "sections": len(sections),
            "section_counts": section_counts,
            "total_cards": len(cards),
            "unresolved_count": len(unresolved),
        }
        return ParseResult(cards=cards, unresolved_anomalies=unresolved, report=report)

    def _extract_text(self, file_path: Path) -> str:
        cmd = ["pdftotext", "-layout", str(file_path), "-"]
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"pdftotext failed: {stderr}")
        return proc.stdout.decode("utf-8", errors="replace")

    def _parse_sections(self, lines: List[str]) -> List[Dict]:
        sections: List[Dict] = []
        mode = "idle"
        current_section: Optional[Dict] = None

        current_question_num: Optional[int] = None
        current_question_stem: List[str] = []
        current_question_options: Dict[str, List[str]] = {}
        current_option_key: Optional[str] = None
        current_question_warnings: List[ParseWarning] = []

        current_answer_num: Optional[int] = None
        current_answer_key: Optional[str] = None
        current_answer_text_lines: List[str] = []
        current_expl_lines: List[str] = []
        current_answer_warnings: List[ParseWarning] = []
        in_explanation = False

        def flush_question() -> None:
            nonlocal current_question_num, current_question_stem, current_question_options, current_option_key, current_question_warnings
            if current_section is None or current_question_num is None:
                return

            options: Dict[str, Dict[str, str]] = {}
            for key, parts in current_question_options.items():
                raw = "\n".join([p for p in parts if p]).strip()
                if raw:
                    options[key] = {"raw": raw, "norm": normalize_text(raw)}

            question_raw = "\n".join([x for x in current_question_stem if x]).strip()
            current_section["questions"][current_question_num] = {
                "question_raw": question_raw,
                "options": options,
                "warnings": list(current_question_warnings),
            }

            current_question_num = None
            current_question_stem = []
            current_question_options = {}
            current_option_key = None
            current_question_warnings = []

        def flush_answer() -> None:
            nonlocal current_answer_num, current_answer_key, current_answer_text_lines, current_expl_lines, current_answer_warnings, in_explanation
            if current_section is None or current_answer_num is None:
                return
            current_section["answers"][current_answer_num] = {
                "answer_key": current_answer_key,
                "answer_text_lines": [x for x in current_answer_text_lines if x],
                "explanation_lines": [x for x in current_expl_lines if x],
                "warnings": list(current_answer_warnings),
            }
            current_answer_num = None
            current_answer_key = None
            current_answer_text_lines = []
            current_expl_lines = []
            current_answer_warnings = []
            in_explanation = False

        section_idx = 0
        for i, raw_line in enumerate(lines):
            line = raw_line.replace("\x0c", "").rstrip("\n")
            stripped = line.strip()

            if stripped.upper() == "QUESTIONS":
                if mode == "questions":
                    flush_question()
                elif mode == "answers":
                    flush_answer()
                section_idx += 1
                title = self._find_section_title(lines, i)
                chapter_number = section_idx if "bonus" not in title.lower() else None
                current_section = {
                    "index": section_idx,
                    "title": title,
                    "chapter_number": chapter_number,
                    "questions": {},
                    "answers": {},
                }
                sections.append(current_section)
                mode = "questions"
                continue

            if stripped.upper() == "ANSWERS":
                if mode == "questions":
                    flush_question()
                if mode == "answers":
                    flush_answer()
                mode = "answers"
                continue

            if stripped.upper() == "REFERENCES":
                if mode == "questions":
                    flush_question()
                if mode == "answers":
                    flush_answer()
                mode = "idle"
                continue

            if current_section is None:
                continue

            if mode == "questions":
                m_q = QUESTION_RE.match(line)
                if m_q:
                    flush_question()
                    current_question_num = int(m_q.group(1))
                    remainder = (m_q.group(2) or "").strip()
                    current_question_stem = [remainder] if remainder else []
                    current_question_options = {}
                    current_option_key = None
                    current_question_warnings = []
                    if ":" not in line:
                        current_question_warnings.append(
                            ParseWarning(
                                code="MISSING_COLON_HEADER_RESOLVED",
                                detail=f"Question header parsed without colon: '{line.strip()}'",
                            )
                        )
                    continue

                if current_question_num is None:
                    continue

                if self._is_noise_line(stripped):
                    continue

                m_opt = OPTION_RE.match(line)
                if m_opt:
                    label = m_opt.group(1).upper()
                    content = (m_opt.group(2) or "").strip()
                    if re.match(r"^\s*[A-E]\s+\.\s*", line):
                        current_question_warnings.append(
                            ParseWarning(
                                code="SPACED_OPTION_LABEL_RESOLVED",
                                detail=f"Option label used spaced dot style: '{line.strip()}'",
                            )
                        )
                    current_option_key = label
                    current_question_options.setdefault(label, [])
                    if content:
                        current_question_options[label].append(content)
                    continue

                if current_option_key is not None and stripped:
                    current_question_options[current_option_key].append(stripped)
                elif stripped:
                    current_question_stem.append(stripped)

            elif mode == "answers":
                m_std = ANSWER_STD_RE.match(line)
                if m_std:
                    flush_answer()
                    current_answer_num = int(m_std.group(1))
                    current_answer_key = m_std.group(2).upper()
                    answer_rest = (m_std.group(3) or "").strip()
                    current_answer_text_lines = [answer_rest] if answer_rest else []
                    current_expl_lines = []
                    current_answer_warnings = []
                    in_explanation = False
                    continue

                m_open = ANSWER_OPEN_RE.match(line)
                if m_open:
                    flush_answer()
                    current_answer_num = int(m_open.group(1))
                    current_answer_key = None
                    current_answer_text_lines = []
                    current_expl_lines = []
                    current_answer_warnings = []
                    in_explanation = False
                    continue

                if current_answer_num is None:
                    continue

                if stripped.lower() == "explanation:":
                    in_explanation = True
                    continue

                if self._is_noise_line(stripped):
                    continue

                if not in_explanation:
                    if current_answer_key is None:
                        m_row = OPTION_RE.match(line)
                        if m_row:
                            current_answer_key = m_row.group(1).upper()
                            row_text = (m_row.group(2) or "").strip()
                            if row_text:
                                current_answer_text_lines.append(row_text)
                            current_answer_warnings.append(
                                ParseWarning(
                                    code="TABLE_ANSWER_RESOLVED",
                                    detail=f"Answer key resolved from option row near question {current_answer_num}.",
                                )
                            )
                    elif stripped:
                        current_answer_text_lines.append(stripped)
                else:
                    current_expl_lines.append(stripped)

        if mode == "questions":
            flush_question()
        if mode == "answers":
            flush_answer()

        return sections

    def _is_noise_line(self, stripped: str) -> bool:
        if not stripped:
            return True
        if stripped in {"�", "嘷", "...", ".."}:
            return True
        if stripped.isdigit():
            return True
        if "TRANSFUSION MEDICINE SELF-ASSESSMENT AND REVIEW" in stripped:
            return True
        if stripped.startswith("In: Schmidt AE, Sullivan HC"):
            return True
        if re.match(r"^[A-Z][A-Z /,&\-]+\s+\d+$", stripped):
            if "QUESTION" not in stripped and "ANSWER" not in stripped:
                return True
        return False

    def _find_section_title(self, lines: List[str], idx: int) -> str:
        for j in range(idx - 1, max(-1, idx - 60), -1):
            candidate = lines[j].replace("\x0c", "").strip()
            if not candidate:
                continue
            if self._is_noise_line(candidate):
                continue
            if candidate.upper() in {"QUESTIONS", "ANSWERS", "REFERENCES"}:
                continue
            if re.match(r"^\d+\.$", candidate):
                continue
            return candidate
        return f"Section {idx}"
