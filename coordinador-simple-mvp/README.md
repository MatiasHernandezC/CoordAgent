# Coordinador Simple MVP

Version inicial del MVP para coordinar reuniones con React y FastAPI.

## Backend

PowerShell:
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

## Frontend

PowerShell:
cd frontend
npm install
npm run dev
