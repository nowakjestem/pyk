#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p data models
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi
# Match bind-mounted directory ownership without needing root or changing other services.
config_uid_lines=$(sed -n '/^APP_UID=/p' .env)
if [ -z "$config_uid_lines" ]; then
  printf '\nAPP_UID=%s\nAPP_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
fi
printf '%s\n' 'Uzupełnij .env (w tym OPENAI_API_KEY), następnie: docker compose build && docker compose run --rm --no-deps bot check --integrations --tools'
