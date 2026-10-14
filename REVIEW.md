# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.

# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or put these in a local .env
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup (idempotent). Interactive docs are at http://localhost:8000/docs.

## Notes
- Instants are epoch milliseconds in the API and BSON dates (UTC) in MongoDB. The client runs with `tz_aware=True`.
- Business rules R1-R5 live in small pure helpers (`attendance_date`, `shift_window`, `compute_*`, `derive_fields`).
- Analytics are MongoDB aggregation pipelines. Each has a `*_pipeline()` builder that `/admin/explain` reuses, so the explained query is exactly the one the endpoint runs.
- Half-up rounding uses `Decimal` in Python and Decimal128 inside pipelines, because MongoDB's `$round` is banker's rounding.
- Concurrency: unique indexes for create and punch-in, an atomic `find_one_and_update` guard for punch-out, and optimistic concurrency for PATCH (a conflict gives 409).

## Judgment calls
- Trend: a range longer than 92 days (inclusive) gives 422.
- PATCH: a request is "no change" (422) when, after recomputing, no stored field would differ.
- `present_days` counts weekday presence records; it does not additionally filter records dated before `joined_on`.