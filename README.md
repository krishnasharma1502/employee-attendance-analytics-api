# Employee Attendance & Analytics API

FastAPI and MongoDB API for employee attendance, punch-in/punch-out, regularization, and attendance analytics.

## Requirements

- Python 3.10+
- MongoDB 6.0+
- Dependencies from `requirements.txt`

## Setup — Windows PowerShell

Run these commands from the `candidate_kit` directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file in the project directory with your own MongoDB connection details:

```env
MONGO_URI=your_mongodb_connection_string
MONGO_DB=attendance_db
```

Do not commit `.env` or any credentials.

Seed the sample data if required:

```powershell
python sample_seed.py
```

Start the API on the required port:

```powershell
uvicorn app.main:app --port 8000
```

## Verify

- Health: http://127.0.0.1:8000/health
- Interactive API documentation: http://127.0.0.1:8000/docs

## Submission

The public Git repository contains `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README.

Do not commit `.env`, credentials, or other secrets.
