from __future__ import annotations

import os
from pathlib import Path


class Config:
    BACKEND_ROOT = Path(__file__).resolve().parents[1]
    PROJECT_ROOT = BACKEND_ROOT.parent
    DATA_DIR = Path(os.environ.get("FLASH_DATA_DIR") or (PROJECT_ROOT / "data"))
    DB_PATH = Path(os.environ.get("FLASH_DB_PATH") or (DATA_DIR / "flashcards.db"))
    UPLOAD_DIR = Path(os.environ.get("FLASH_UPLOAD_DIR") or (DATA_DIR / "uploads"))
    ARTIFACT_DIR = Path(os.environ.get("FLASH_ARTIFACT_DIR") or (DATA_DIR / "artifacts"))
    FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"

    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-secret-change-me"
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH") or str(32 * 1024 * 1024))


class TestConfig(Config):
    TESTING = True
