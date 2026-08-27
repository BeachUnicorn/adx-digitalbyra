# ADX – development commands (uv-based, no pip)

export DJANGO_SETTINGS_MODULE := "config.settings.development"

# Install/sync the locked environment (creates .venv if needed)
install:
    uv sync

# Start dev server
serve:
    uv run python manage.py runserver 127.0.0.1:8000

migrate:
    uv run python manage.py migrate

makemigrations *ARGS:
    uv run python manage.py makemigrations {{ARGS}}

createsuperuser:
    uv run python manage.py createsuperuser

shell:
    uv run python manage.py shell

check:
    uv run python manage.py check

collectstatic:
    uv run python manage.py collectstatic --noinput

lint:
    uv run ruff check .

format:
    uv run ruff format .

# Add a dependency and update the lock file
add PACKAGE:
    uv add {{PACKAGE}}

# Refresh the lock file from pyproject.toml
lock:
    uv lock

# Build the Tiptap editor bundle (production)
build-js:
    npm run build

# Rebuild the Tiptap bundle on change (development)
watch-js:
    npm run watch

# Export current DB content + referenced media into the tracked seed_data/
# folder, ready to commit and push (see server/SEED_CONTENT.md).
export-data:
    uv run python manage.py export_site_data

# Import seed_data/ into THIS environment's DB + media (asks for confirmation).
# Run on the server after a git pull. Overwrites current content.
import-data:
    uv run python manage.py import_site_data
