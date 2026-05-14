#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py migrate
python manage.py ensure_superuser
python manage.py verify_existing_users
python manage.py collectstatic --noinput
python manage.py fix_empty_slugs
