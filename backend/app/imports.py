from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict

from flask import Flask

from .db import get_db
from .parsers import AABBPdfParser, CsvCardParser
from .utils import now_iso_utc, to_json


class ImportProcessor:
    def __init__(self, app: Flask) -> None:
        self.app = app

    def start(self, job_id: int, file_path: Path, source_type: str) -> None:
        thread = threading.Thread(
            target=self._process_job,
            args=(job_id, file_path, source_type),
            daemon=True,
            name=f"import-job-{job_id}",
        )
        thread.start()

    def _parser_for(self, source_type: str):
        if source_type == "pdf":
            return AABBPdfParser()
        if source_type == "csv":
            return CsvCardParser()
        raise ValueError(f"Unsupported source_type: {source_type}")

    def _fetch_job(self, job_id: int) -> Dict:
        with get_db() as conn:
            row = conn.execute(
                """
                SELECT id, resource_id, resource_version_id, filename, source_type, status
                FROM import_jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Import job {job_id} not found")
        return dict(row)

    def _mark_failed(self, job_id: int, resource_version_id: int, summary: str, report: Dict) -> None:
        now = now_iso_utc()
        with get_db() as conn:
            conn.execute(
                """
                UPDATE import_jobs
                SET status = 'failed', error_summary = ?, anomaly_report_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (summary[:4000], to_json(report), now, job_id),
            )
            conn.execute(
                """
                UPDATE resource_versions
                SET status = 'failed'
                WHERE id = ?
                """,
                (resource_version_id,),
            )

    def _mark_processing(self, job_id: int) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE import_jobs SET status = 'processing', error_summary = NULL WHERE id = ?",
                (job_id,),
            )

    def _mark_success(self, job_id: int, resource_version_id: int, report: Dict) -> None:
        now = now_iso_utc()
        with get_db() as conn:
            conn.execute(
                """
                UPDATE import_jobs
                SET status = 'succeeded', error_summary = NULL, anomaly_report_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (to_json(report), now, job_id),
            )
            conn.execute(
                """
                UPDATE resource_versions
                SET status = 'draft'
                WHERE id = ?
                """,
                (resource_version_id,),
            )

    def _process_job(self, job_id: int, file_path: Path, source_type: str) -> None:
        with self.app.app_context():
            try:
                job = self._fetch_job(job_id)
                resource_version_id = int(job["resource_version_id"])
                self._mark_processing(job_id)
                parser = self._parser_for(source_type)
                result = parser.parse(file_path)

                if result.unresolved_anomalies:
                    report = {
                        **result.report,
                        "unresolved_anomalies": result.unresolved_anomalies,
                    }
                    summary = f"Strict validation failed with {len(result.unresolved_anomalies)} unresolved anomalies."
                    self._mark_failed(job_id, resource_version_id, summary, report)
                    return

                with get_db() as conn:
                    now = now_iso_utc()
                    conn.execute("DELETE FROM cards WHERE resource_version_id = ?", (resource_version_id,))

                    for card in result.cards:
                        cur = conn.execute(
                            """
                            INSERT INTO cards (
                                resource_version_id,
                                external_card_key,
                                chapter,
                                question_number,
                                question_raw,
                                question_norm,
                                answer_key,
                                answer_text_raw,
                                answer_text_norm,
                                explanation_raw,
                                explanation_norm,
                                state,
                                created_at,
                                updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                            """,
                            (
                                resource_version_id,
                                card.external_card_key,
                                card.chapter_title,
                                card.question_number,
                                card.question_raw,
                                card.question_norm,
                                card.answer_key,
                                card.answer_text_raw,
                                card.answer_text_norm,
                                card.explanation_raw,
                                card.explanation_norm,
                                now,
                                now,
                            ),
                        )
                        card_id = int(cur.lastrowid)

                        for option_key, payload in sorted(card.options.items()):
                            conn.execute(
                                """
                                INSERT INTO card_options (card_id, option_key, text_raw, text_norm)
                                VALUES (?, ?, ?, ?)
                                """,
                                (card_id, option_key, payload["raw"], payload["norm"]),
                            )

                        for warning in card.warnings:
                            conn.execute(
                                """
                                INSERT INTO card_parse_warnings (card_id, warning_code, warning_detail)
                                VALUES (?, ?, ?)
                                """,
                                (card_id, warning.code, warning.detail),
                            )

                self._mark_success(job_id, resource_version_id, result.report)
            except Exception as exc:  # noqa: BLE001 - ensure job state updates on any failure
                try:
                    job = self._fetch_job(job_id)
                    resource_version_id = int(job["resource_version_id"])
                    report = {
                        "source": source_type,
                        "exception": str(exc),
                    }
                    self._mark_failed(job_id, resource_version_id, str(exc), report)
                except Exception:
                    return
