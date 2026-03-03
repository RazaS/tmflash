# Flashcard App v1

Multi-user flashcard platform with strict ingestion and draft-to-publish workflow.

## Stack
- Backend: Flask + SQLite (`backend/`)
- Frontend: React + Vite (`frontend/`)
- Storage: `data/` (`flashcards.db`, uploads, artifacts)

## Features Implemented
- Auth: `POST /api/signup`, `POST /api/login`, `POST /api/logout`, `GET /api/me`
- Upload-driven imports: `POST /api/resources/upload`
- Import jobs + background processing: `GET /api/import-jobs/:id`
- Resource/version browsing:
  - `GET /api/resources`
  - `GET /api/resources/:id/versions`
  - `GET /api/resource-versions/:id/drafts`
- Draft editing + publishing:
  - `POST /api/cards/:id`
  - `POST /api/resource-versions/:id/publish`
- Study loop:
  - `GET /api/study/next`
  - `POST /api/study/grade`
  - `GET /api/study/progress`
  - `POST /api/cards/:id/archive`

## Parsers
- `AABBPdfParser` for AABB-style PDFs with strict validation gates and known edge-case handling.
- `CsvCardParser` for bulk CSV imports.

Strict gate behavior:
- Any unresolved anomaly fails the import job.
- Failed imports do not persist cards.

## Local Run

### Backend
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
python backend/run.py
```

Backend runs on `http://localhost:5000`.

### Frontend (dev)
```bash
cd frontend
npm install
npm run dev
```

Vite dev server runs on `http://localhost:5173` and proxies `/api` to backend.

### Frontend (build for Flask static serving)
```bash
cd frontend
npm run build
```

Flask serves built assets from `frontend/dist`.

## Tests
```bash
. .venv/bin/activate
cd backend
pytest -q
```

Includes parser/API tests and end-to-end API flow tests.
The AABB PDF regression test auto-skips if `AABB Self Assessment - Copy.pdf` is not present.
