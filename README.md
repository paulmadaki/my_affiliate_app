# My Affiliate App

## Setup

1. Create and activate the virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. Copy the example environment file:

```powershell
copy .env.example .env
```

4. Update `.env` with your configuration:

- `SECRET_KEY` must be a secure random string.
- `DATABASE_URL` can be `sqlite:///local.db` for local development or your production database URL.
- `PAYSTACK_SECRET` is required for payment activation.
- `ADMIN_EMAIL` is the admin login email.
- `FLASK_ENV=development` for local development.

## Database migrations

Use Flask-Migrate to create and apply schema changes.

```powershell
.\.venv\Scripts\python.exe -m flask --app app db migrate -m "Add description"
.\.venv\Scripts\python.exe -m flask --app app db upgrade
```

The project already includes a working migration setup in `migrations/`.

## Run the app locally

```powershell
.\.venv\Scripts\python.exe -m flask --app app run
```

Or use the production server:

```powershell
gunicorn app:app
```

## Production notes

- In production, set `SECRET_KEY`, `DATABASE_URL`, and `PAYSTACK_SECRET` in the environment.
- Do not commit `.env` or any secret values.
- Use `gunicorn app:app` behind a reverse proxy.
