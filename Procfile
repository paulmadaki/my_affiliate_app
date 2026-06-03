release: flask db upgrade
web: gunicorn app:app
web: gunicorn app:app --workers 3 --timeout 120