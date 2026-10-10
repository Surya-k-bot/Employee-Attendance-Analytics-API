# Employee Attendance & Analytics API

FastAPI service backed by MongoDB, implementing the API contract in `openapi.yaml`. Requires Python 3.11+ and MongoDB 6.0+.
Install dependencies with `pip install -r requirements.txt`, set `MONGO_URI` and `MONGO_DB` (or use the local defaults),
then run from this directory with `uvicorn app.main:app --port 8000`. Indexes are created automatically at startup.
Interactive API documentation is served at `/docs`. The assignment's sample data and hiring brief are intentionally not
included in this public repository. Do not commit `.env` files or database credentials.
