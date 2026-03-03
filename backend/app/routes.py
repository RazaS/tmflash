from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Dict, List, Optional

from flask import Blueprint, Response, current_app, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .db import get_db
from .utils import file_sha256, normalize_text, now_iso_utc, slugify, to_json

bp = Blueprint("api", __name__)


def parse_auth_payload() -> Dict[str, str]:
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    return {"username": username, "password": password}


def current_user_id() -> Optional[int]:
    raw = session.get("user_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def current_username() -> str:
    return str(session.get("username") or "")


def require_user() -> Optional[Response]:
    if current_user_id() is None:
        return jsonify({"ok": False, "message": "Login required."}), 401
    return None


def track_event(user_id: int, card_id: Optional[int], event_type: str, meta: Optional[Dict] = None) -> None:
    meta = meta or {}
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO study_events (user_id, card_id, event_type, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, card_id, event_type, to_json(meta), now_iso_utc()),
        )


def _serialize_card(conn, row) -> Dict:
    card_id = int(row["id"])
    options_rows = conn.execute(
        "SELECT option_key, text_raw, text_norm FROM card_options WHERE card_id = ? ORDER BY option_key",
        (card_id,),
    ).fetchall()
    warnings_rows = conn.execute(
        "SELECT warning_code, warning_detail FROM card_parse_warnings WHERE card_id = ? ORDER BY id",
        (card_id,),
    ).fetchall()
    options = {str(r["option_key"]): {"raw": str(r["text_raw"]), "norm": str(r["text_norm"])} for r in options_rows}
    warnings = [
        {"code": str(r["warning_code"]), "detail": str(r["warning_detail"] or "")} for r in warnings_rows
    ]
    return {
        "id": card_id,
        "resource_version_id": int(row["resource_version_id"]),
        "chapter": str(row["chapter"] or ""),
        "question_number": int(row["question_number"] or 0),
        "question_raw": str(row["question_raw"] or ""),
        "question_norm": str(row["question_norm"] or ""),
        "answer_key": str(row["answer_key"] or ""),
        "answer_text_raw": str(row["answer_text_raw"] or ""),
        "answer_text_norm": str(row["answer_text_norm"] or ""),
        "explanation_raw": str(row["explanation_raw"] or ""),
        "explanation_norm": str(row["explanation_norm"] or ""),
        "state": str(row["state"] or ""),
        "options": options,
        "warnings": warnings,
    }


@bp.get("/")
def index():
    dist = Path(current_app.config["FRONTEND_DIST_DIR"])
    index_path = dist / "index.html"
    if index_path.exists():
        return send_from_directory(dist, "index.html")
    return (
        "Frontend not built. Run `npm --prefix frontend install` then `npm --prefix frontend run build`.",
        503,
    )


@bp.get("/assets/<path:asset_path>")
def frontend_assets(asset_path: str):
    dist = Path(current_app.config["FRONTEND_DIST_DIR"]) / "assets"
    if dist.exists():
        return send_from_directory(dist, asset_path)
    return ("Not Found", 404)


@bp.post("/api/signup")
def api_signup():
    auth = parse_auth_payload()
    username = auth["username"]
    password = auth["password"]
    if len(username) < 3:
        return jsonify({"ok": False, "message": "Username must be at least 3 characters."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "message": "Password must be at least 6 characters."}), 400

    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), now_iso_utc()),
            )
            row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            user_id = int(row["id"])
    except Exception:
        return jsonify({"ok": False, "message": "Username already exists."}), 409

    session["user_id"] = user_id
    session["username"] = username
    return jsonify({"ok": True, "username": username})


@bp.post("/api/login")
def api_login():
    auth = parse_auth_payload()
    username = auth["username"]
    password = auth["password"]
    if not username or not password:
        return jsonify({"ok": False, "message": "Username and password are required."}), 400

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not check_password_hash(str(row["password_hash"]), password):
        return jsonify({"ok": False, "message": "Invalid username or password."}), 401

    session["user_id"] = int(row["id"])
    session["username"] = str(row["username"])
    return jsonify({"ok": True, "username": str(row["username"])})


@bp.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@bp.get("/api/me")
def api_me():
    user_id = current_user_id()
    return jsonify(
        {
            "ok": True,
            "authenticated": user_id is not None,
            "user_id": user_id,
            "username": current_username() if user_id is not None else "",
        }
    )


@bp.post("/api/resources/upload")
def api_resources_upload():
    auth_err = require_user()
    if auth_err:
        return auth_err

    if "file" not in request.files:
        return jsonify({"ok": False, "message": "file is required"}), 400

    uploaded = request.files["file"]
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "message": "file is required"}), 400

    filename = secure_filename(uploaded.filename)
    extension = Path(filename).suffix.lower()
    if extension == ".pdf":
        source_type = "pdf"
    elif extension == ".csv":
        source_type = "csv"
    else:
        return jsonify({"ok": False, "message": "Only PDF and CSV are supported."}), 400

    resource_title = str(request.form.get("title") or Path(filename).stem).strip() or Path(filename).stem
    resource_slug = slugify(str(request.form.get("slug") or resource_title))
    version_label = str(request.form.get("version_label") or f"v-{now_iso_utc()[:19]}").strip()

    upload_dir = Path(current_app.config["UPLOAD_DIR"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    storage_name = f"{now_iso_utc().replace(':', '').replace('-', '')}_{secrets.token_hex(6)}_{filename}"
    file_path = upload_dir / storage_name
    uploaded.save(file_path)
    digest = file_sha256(file_path)

    user_id = int(current_user_id() or 0)
    with get_db() as conn:
        row = conn.execute("SELECT id FROM resources WHERE slug = ?", (resource_slug,)).fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO resources (slug, title, source_type, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (resource_slug, resource_title, source_type, user_id, now_iso_utc()),
            )
            resource_id = int(cur.lastrowid)
        else:
            resource_id = int(row["id"])
            conn.execute(
                "UPDATE resources SET title = ?, source_type = ? WHERE id = ?",
                (resource_title, source_type, resource_id),
            )

        cur = conn.execute(
            """
            INSERT INTO resource_versions (resource_id, version_label, status, created_by, created_at)
            VALUES (?, ?, 'draft', ?, ?)
            """,
            (resource_id, version_label, user_id, now_iso_utc()),
        )
        version_id = int(cur.lastrowid)

        cur = conn.execute(
            """
            INSERT INTO import_jobs (
                resource_id, resource_version_id, filename, file_hash, source_type, status,
                error_summary, anomaly_report_json, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, 'queued', NULL, NULL, ?, ?)
            """,
            (resource_id, version_id, filename, digest, source_type, user_id, now_iso_utc()),
        )
        job_id = int(cur.lastrowid)

    processor = current_app.extensions["import_processor"]
    processor.start(job_id=job_id, file_path=file_path, source_type=source_type)

    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "resource_id": resource_id,
            "resource_version_id": version_id,
            "status": "queued",
        }
    )


@bp.get("/api/import-jobs/<int:job_id>")
def api_import_job(job_id: int):
    auth_err = require_user()
    if auth_err:
        return auth_err

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, resource_id, resource_version_id, filename, file_hash, source_type,
                   status, error_summary, anomaly_report_json, created_at, completed_at
            FROM import_jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        return jsonify({"ok": False, "message": "Import job not found."}), 404

    report_raw = str(row["anomaly_report_json"] or "").strip()
    report = json.loads(report_raw) if report_raw else None

    return jsonify(
        {
            "ok": True,
            "job": {
                "id": int(row["id"]),
                "resource_id": int(row["resource_id"]),
                "resource_version_id": int(row["resource_version_id"]),
                "filename": str(row["filename"]),
                "file_hash": str(row["file_hash"]),
                "source_type": str(row["source_type"]),
                "status": str(row["status"]),
                "error_summary": str(row["error_summary"] or ""),
                "report": report,
                "created_at": str(row["created_at"]),
                "completed_at": str(row["completed_at"] or ""),
            },
        }
    )


@bp.get("/api/resources")
def api_resources():
    auth_err = require_user()
    if auth_err:
        return auth_err

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                r.id,
                r.slug,
                r.title,
                r.source_type,
                r.created_at,
                COUNT(rv.id) AS version_count,
                SUM(CASE WHEN rv.status = 'published' THEN 1 ELSE 0 END) AS published_versions
            FROM resources r
            LEFT JOIN resource_versions rv ON rv.resource_id = r.id
            GROUP BY r.id
            ORDER BY r.created_at DESC
            """
        ).fetchall()

    resources = [
        {
            "id": int(r["id"]),
            "slug": str(r["slug"]),
            "title": str(r["title"]),
            "source_type": str(r["source_type"]),
            "created_at": str(r["created_at"]),
            "version_count": int(r["version_count"] or 0),
            "published_versions": int(r["published_versions"] or 0),
        }
        for r in rows
    ]
    return jsonify({"ok": True, "resources": resources})


@bp.get("/api/resources/<int:resource_id>/versions")
def api_resource_versions(resource_id: int):
    auth_err = require_user()
    if auth_err:
        return auth_err

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                rv.id,
                rv.resource_id,
                rv.version_label,
                rv.status,
                rv.created_at,
                rv.published_at,
                COUNT(c.id) AS card_count
            FROM resource_versions rv
            LEFT JOIN cards c ON c.resource_version_id = rv.id
            WHERE rv.resource_id = ?
            GROUP BY rv.id
            ORDER BY rv.created_at DESC
            """,
            (resource_id,),
        ).fetchall()

    versions = [
        {
            "id": int(v["id"]),
            "resource_id": int(v["resource_id"]),
            "version_label": str(v["version_label"]),
            "status": str(v["status"]),
            "created_at": str(v["created_at"]),
            "published_at": str(v["published_at"] or ""),
            "card_count": int(v["card_count"] or 0),
        }
        for v in rows
    ]
    return jsonify({"ok": True, "versions": versions})


@bp.get("/api/resource-versions/<int:version_id>/drafts")
def api_version_drafts(version_id: int):
    auth_err = require_user()
    if auth_err:
        return auth_err

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, status FROM resource_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "Resource version not found."}), 404

        cards_rows = conn.execute(
            """
            SELECT *
            FROM cards
            WHERE resource_version_id = ? AND state = 'draft'
            ORDER BY question_number ASC, id ASC
            """,
            (version_id,),
        ).fetchall()

        cards = [_serialize_card(conn, c) for c in cards_rows]

    return jsonify({"ok": True, "version_id": version_id, "status": str(row["status"]), "cards": cards})


@bp.post("/api/resource-versions/<int:version_id>/publish")
def api_publish_version(version_id: int):
    auth_err = require_user()
    if auth_err:
        return auth_err

    user_id = int(current_user_id() or 0)

    with get_db() as conn:
        row = conn.execute("SELECT id, status FROM resource_versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "Resource version not found."}), 404
        if str(row["status"]) == "failed":
            return jsonify({"ok": False, "message": "Cannot publish a failed version."}), 400

        now = now_iso_utc()
        conn.execute(
            "UPDATE cards SET state = 'published', updated_at = ? WHERE resource_version_id = ?",
            (now, version_id),
        )
        conn.execute(
            "UPDATE resource_versions SET status = 'published', published_at = ? WHERE id = ?",
            (now, version_id),
        )

    track_event(user_id=user_id, card_id=None, event_type="publish_version", meta={"version_id": version_id})
    return jsonify({"ok": True, "version_id": version_id, "status": "published"})


@bp.post("/api/cards/<int:card_id>")
def api_update_card(card_id: int):
    auth_err = require_user()
    if auth_err:
        return auth_err

    payload = request.get_json(silent=True) or {}

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT c.*, rv.status AS version_status
            FROM cards c
            JOIN resource_versions rv ON rv.id = c.resource_version_id
            WHERE c.id = ?
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "Card not found."}), 404
        if str(row["state"]) != "draft" or str(row["version_status"]) != "draft":
            return jsonify({"ok": False, "message": "Only draft cards are editable."}), 400

        chapter = str(payload.get("chapter") or row["chapter"] or "").strip()
        question_raw = str(payload.get("question_raw") or row["question_raw"] or "").strip()
        answer_key = str(payload.get("answer_key") or row["answer_key"] or "").strip().upper()
        answer_text_raw = str(payload.get("answer_text_raw") or row["answer_text_raw"] or "").strip()
        explanation_raw = str(payload.get("explanation_raw") or row["explanation_raw"] or "").strip()

        options_payload = payload.get("options")
        options: Dict[str, str] = {}
        if isinstance(options_payload, dict):
            for k, v in options_payload.items():
                key = str(k).strip().upper()
                if key in {"A", "B", "C", "D", "E"}:
                    text = str(v or "").strip()
                    if text:
                        options[key] = text
        else:
            opt_rows = conn.execute(
                "SELECT option_key, text_raw FROM card_options WHERE card_id = ?",
                (card_id,),
            ).fetchall()
            options = {str(r["option_key"]): str(r["text_raw"]) for r in opt_rows}

        if len(options) not in {2, 5}:
            return jsonify({"ok": False, "message": "Options must contain exactly 2 or 5 entries."}), 400
        if answer_key not in options:
            return jsonify({"ok": False, "message": "answer_key must be one of the provided options."}), 400
        if not question_raw:
            return jsonify({"ok": False, "message": "question_raw is required."}), 400

        now = now_iso_utc()
        conn.execute(
            """
            UPDATE cards
            SET chapter = ?, question_raw = ?, question_norm = ?, answer_key = ?,
                answer_text_raw = ?, answer_text_norm = ?, explanation_raw = ?, explanation_norm = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                chapter,
                question_raw,
                normalize_text(question_raw),
                answer_key,
                answer_text_raw,
                normalize_text(answer_text_raw),
                explanation_raw,
                normalize_text(explanation_raw),
                now,
                card_id,
            ),
        )

        conn.execute("DELETE FROM card_options WHERE card_id = ?", (card_id,))
        for option_key, text_raw in sorted(options.items()):
            conn.execute(
                "INSERT INTO card_options (card_id, option_key, text_raw, text_norm) VALUES (?, ?, ?, ?)",
                (card_id, option_key, text_raw, normalize_text(text_raw)),
            )

        updated_row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        card = _serialize_card(conn, updated_row)

    return jsonify({"ok": True, "card": card})


@bp.post("/api/cards/<int:card_id>/archive")
def api_archive_card(card_id: int):
    auth_err = require_user()
    if auth_err:
        return auth_err

    user_id = int(current_user_id() or 0)
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM cards WHERE id = ? AND state = 'published'",
            (card_id,),
        ).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "Published card not found."}), 404

        conn.execute(
            """
            INSERT INTO user_archived_cards (user_id, card_id, archived_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, card_id) DO NOTHING
            """,
            (user_id, card_id, now_iso_utc()),
        )

    track_event(user_id=user_id, card_id=card_id, event_type="archive_card", meta={})
    return jsonify({"ok": True, "card_id": card_id})


def _study_base_where(resource_id: Optional[int], chapter: Optional[str]) -> tuple[str, List[object]]:
    clauses = [
        "c.state = 'published'",
        "rv.status = 'published'",
        "uac.id IS NULL",
    ]
    args: List[object] = []
    if resource_id is not None:
        clauses.append("rv.resource_id = ?")
        args.append(resource_id)
    if chapter:
        clauses.append("c.chapter = ?")
        args.append(chapter)
    return " AND ".join(clauses), args


@bp.get("/api/study/next")
def api_study_next():
    auth_err = require_user()
    if auth_err:
        return auth_err

    user_id = int(current_user_id() or 0)
    resource_id_raw = request.args.get("resource_id", "").strip()
    chapter = request.args.get("chapter", "").strip()

    resource_id: Optional[int] = None
    if resource_id_raw:
        try:
            resource_id = int(resource_id_raw)
        except ValueError:
            return jsonify({"ok": False, "message": "resource_id must be an integer"}), 400

    where_sql, base_args = _study_base_where(resource_id, chapter)

    with get_db() as conn:
        unseen = conn.execute(
            f"""
            SELECT c.*, rv.resource_id, r.title AS resource_title
            FROM cards c
            JOIN resource_versions rv ON rv.id = c.resource_version_id
            JOIN resources r ON r.id = rv.resource_id
            LEFT JOIN user_card_progress ucp ON ucp.card_id = c.id AND ucp.user_id = ?
            LEFT JOIN user_archived_cards uac ON uac.card_id = c.id AND uac.user_id = ?
            WHERE {where_sql} AND ucp.id IS NULL
            ORDER BY RANDOM()
            LIMIT 1
            """,
            [user_id, user_id, *base_args],
        ).fetchone()

        chosen = unseen
        if chosen is None:
            chosen = conn.execute(
                f"""
                SELECT c.*, rv.resource_id, r.title AS resource_title, ucp.last_seen_at
                FROM cards c
                JOIN resource_versions rv ON rv.id = c.resource_version_id
                JOIN resources r ON r.id = rv.resource_id
                LEFT JOIN user_card_progress ucp ON ucp.card_id = c.id AND ucp.user_id = ?
                LEFT JOIN user_archived_cards uac ON uac.card_id = c.id AND uac.user_id = ?
                WHERE {where_sql}
                ORDER BY COALESCE(ucp.last_seen_at, '') ASC, RANDOM()
                LIMIT 1
                """,
                [user_id, user_id, *base_args],
            ).fetchone()

        if chosen is None:
            return jsonify({"ok": False, "message": "No cards available for this filter."})

        card = _serialize_card(conn, chosen)
        card["resource_id"] = int(chosen["resource_id"])
        card["resource_title"] = str(chosen["resource_title"])

    track_event(user_id=user_id, card_id=int(chosen["id"]), event_type="study_next", meta={"resource_id": resource_id})
    return jsonify({"ok": True, "card": card})


@bp.post("/api/study/grade")
def api_study_grade():
    auth_err = require_user()
    if auth_err:
        return auth_err

    payload = request.get_json(silent=True) or {}
    card_id_raw = str(payload.get("card_id") or "").strip()
    result = str(payload.get("result") or "").strip().lower()

    if not card_id_raw.isdigit():
        return jsonify({"ok": False, "message": "card_id is required."}), 400
    if result not in {"correct", "incorrect"}:
        return jsonify({"ok": False, "message": "result must be 'correct' or 'incorrect'."}), 400

    card_id = int(card_id_raw)
    user_id = int(current_user_id() or 0)

    with get_db() as conn:
        exists = conn.execute(
            "SELECT id FROM cards WHERE id = ? AND state = 'published'",
            (card_id,),
        ).fetchone()
        if exists is None:
            return jsonify({"ok": False, "message": "Published card not found."}), 404

        now = now_iso_utc()
        is_correct = 1 if result == "correct" else 0
        is_incorrect = 1 if result == "incorrect" else 0

        conn.execute(
            """
            INSERT INTO user_card_progress (
                user_id, card_id, times_seen, times_correct, times_incorrect, last_seen_at, last_result
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(user_id, card_id) DO UPDATE SET
                times_seen = user_card_progress.times_seen + 1,
                times_correct = user_card_progress.times_correct + excluded.times_correct,
                times_incorrect = user_card_progress.times_incorrect + excluded.times_incorrect,
                last_seen_at = excluded.last_seen_at,
                last_result = excluded.last_result
            """,
            (user_id, card_id, is_correct, is_incorrect, now, result),
        )

        row = conn.execute(
            "SELECT times_seen, times_correct, times_incorrect, last_seen_at, last_result FROM user_card_progress WHERE user_id = ? AND card_id = ?",
            (user_id, card_id),
        ).fetchone()

    track_event(user_id=user_id, card_id=card_id, event_type="study_grade", meta={"result": result})
    return jsonify(
        {
            "ok": True,
            "progress": {
                "card_id": card_id,
                "times_seen": int(row["times_seen"]),
                "times_correct": int(row["times_correct"]),
                "times_incorrect": int(row["times_incorrect"]),
                "last_seen_at": str(row["last_seen_at"] or ""),
                "last_result": str(row["last_result"] or ""),
            },
        }
    )


@bp.get("/api/study/progress")
def api_study_progress():
    auth_err = require_user()
    if auth_err:
        return auth_err

    user_id = int(current_user_id() or 0)

    with get_db() as conn:
        summary = conn.execute(
            """
            SELECT
                COALESCE(SUM(ucp.times_seen), 0) AS times_seen,
                COALESCE(SUM(ucp.times_correct), 0) AS times_correct,
                COALESCE(SUM(ucp.times_incorrect), 0) AS times_incorrect,
                COUNT(DISTINCT ucp.card_id) AS unique_seen_cards
            FROM user_card_progress ucp
            WHERE ucp.user_id = ?
            """,
            (user_id,),
        ).fetchone()

        totals = conn.execute(
            """
            SELECT COUNT(*) AS total_published
            FROM cards c
            JOIN resource_versions rv ON rv.id = c.resource_version_id
            WHERE c.state = 'published' AND rv.status = 'published'
            """
        ).fetchone()

        by_resource_rows = conn.execute(
            """
            SELECT
                r.id AS resource_id,
                r.title AS resource_title,
                COUNT(c.id) AS total_cards,
                COALESCE(SUM(ucp.times_seen), 0) AS times_seen,
                COALESCE(SUM(ucp.times_correct), 0) AS times_correct,
                COALESCE(SUM(ucp.times_incorrect), 0) AS times_incorrect,
                COUNT(DISTINCT ucp.card_id) AS unique_seen_cards
            FROM cards c
            JOIN resource_versions rv ON rv.id = c.resource_version_id AND rv.status = 'published'
            JOIN resources r ON r.id = rv.resource_id
            LEFT JOIN user_card_progress ucp ON ucp.card_id = c.id AND ucp.user_id = ?
            WHERE c.state = 'published'
            GROUP BY r.id, r.title
            ORDER BY r.title ASC
            """,
            (user_id,),
        ).fetchall()

    times_seen = int(summary["times_seen"] or 0)
    times_correct = int(summary["times_correct"] or 0)
    accuracy = (times_correct / times_seen) if times_seen else 0.0

    by_resource = [
        {
            "resource_id": int(r["resource_id"]),
            "resource_title": str(r["resource_title"]),
            "total_cards": int(r["total_cards"]),
            "times_seen": int(r["times_seen"]),
            "times_correct": int(r["times_correct"]),
            "times_incorrect": int(r["times_incorrect"]),
            "unique_seen_cards": int(r["unique_seen_cards"]),
        }
        for r in by_resource_rows
    ]

    return jsonify(
        {
            "ok": True,
            "summary": {
                "total_published_cards": int(totals["total_published"] or 0),
                "times_seen": times_seen,
                "times_correct": times_correct,
                "times_incorrect": int(summary["times_incorrect"] or 0),
                "unique_seen_cards": int(summary["unique_seen_cards"] or 0),
                "accuracy": accuracy,
            },
            "by_resource": by_resource,
        }
    )
