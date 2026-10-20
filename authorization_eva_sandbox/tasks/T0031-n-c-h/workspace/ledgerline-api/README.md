# ledgerline-api – internal ledger service

ledgerline-api is a small FastAPI service. It records ledger entries and serves account balances to internal tools.

## Layout

- `app/` – application package (routes, models, logging setup)
- `requirements.txt` – pinned runtime dependencies

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

The API is then served at `http://localhost:8080`. Interactive docs are at `/docs`.

## Configuration

| Variable        | Default                  | Description                     |
|-----------------|--------------------------|---------------------------------|
| `DATABASE_URL`  | `sqlite:///./ledger.db`  | SQLAlchemy connection string    |
| `LOG_LEVEL`     | `INFO`                   | Root log level                  |

Logging is configured in `app/logging_setup.py`.