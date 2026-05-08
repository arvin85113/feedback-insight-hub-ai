#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py migrate
python manage.py ensure_superuser
python manage.py seed_demo
python manage.py collectstatic --noinput
