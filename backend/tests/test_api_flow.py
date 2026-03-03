from __future__ import annotations

import io
import time
from pathlib import Path

from app import create_app


def _signup(client, username: str, password: str = "password123"):
    return client.post(
        "/api/signup",
        json={"username": username, "password": password},
    )


def _wait_for_job(client, job_id: int, timeout_s: float = 10.0):
    start = time.time()
    while time.time() - start < timeout_s:
        res = client.get(f"/api/import-jobs/{job_id}")
        data = res.get_json()
        status = data["job"]["status"]
        if status in {"succeeded", "failed"}:
            return data
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for import job")


def test_upload_publish_and_study_isolation(tmp_path: Path):
    db_path = tmp_path / "test.db"
    upload_dir = tmp_path / "uploads"
    artifact_dir = tmp_path / "artifacts"

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DB_PATH": str(db_path),
            "UPLOAD_DIR": str(upload_dir),
            "ARTIFACT_DIR": str(artifact_dir),
            "FRONTEND_DIST_DIR": str(tmp_path / "frontend-dist"),
        }
    )

    client1 = app.test_client()
    resp = _signup(client1, "alice")
    assert resp.status_code == 200

    csv_bytes = (
        "question,option_a,option_b,option_c,option_d,option_e,answer_key,answer_text,explanation,chapter,question_number\n"
        "Capital of France?,Berlin,Paris,Rome,Madrid,London,B,Paris,Geography fact,Geo,1\n"
        "Sky color?,Blue,Green,,,,A,Blue,Observation,Geo,2\n"
    ).encode("utf-8")

    upload_resp = client1.post(
        "/api/resources/upload",
        data={
            "title": "Demo Cards",
            "slug": "demo-cards",
            "version_label": "v1",
            "file": (io.BytesIO(csv_bytes), "demo.csv"),
        },
        content_type="multipart/form-data",
    )
    assert upload_resp.status_code == 200
    upload_data = upload_resp.get_json()
    job_id = int(upload_data["job_id"])

    final_job = _wait_for_job(client1, job_id)
    assert final_job["job"]["status"] == "succeeded"

    res = client1.get("/api/resources")
    resources = res.get_json()["resources"]
    assert len(resources) == 1
    resource_id = int(resources[0]["id"])

    versions_res = client1.get(f"/api/resources/{resource_id}/versions")
    versions = versions_res.get_json()["versions"]
    assert len(versions) == 1
    version_id = int(versions[0]["id"])

    drafts_res = client1.get(f"/api/resource-versions/{version_id}/drafts")
    drafts = drafts_res.get_json()["cards"]
    assert len(drafts) == 2

    publish_res = client1.post(f"/api/resource-versions/{version_id}/publish")
    assert publish_res.status_code == 200

    next_res = client1.get("/api/study/next")
    next_data = next_res.get_json()
    assert next_data["ok"] is True
    card_id = int(next_data["card"]["id"])

    grade_res = client1.post("/api/study/grade", json={"card_id": card_id, "result": "correct"})
    assert grade_res.status_code == 200

    progress_res = client1.get("/api/study/progress")
    progress = progress_res.get_json()["summary"]
    assert progress["times_seen"] == 1

    client2 = app.test_client()
    resp2 = _signup(client2, "bob")
    assert resp2.status_code == 200

    progress2 = client2.get("/api/study/progress").get_json()["summary"]
    assert progress2["times_seen"] == 0

    next2 = client2.get("/api/study/next").get_json()
    assert next2["ok"] is True
