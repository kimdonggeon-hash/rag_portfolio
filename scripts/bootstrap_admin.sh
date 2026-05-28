#!/usr/bin/env bash
set -euo pipefail

echo "[bootstrap] start"
python -V

# (선택) DB 준비
echo "[bootstrap] migrate"
python manage.py migrate --noinput

# 어드민 강제 생성/갱신
echo "[bootstrap] bootstrap_admin"
python manage.py bootstrap_admin

echo "[bootstrap] done"
