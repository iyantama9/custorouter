#!/bin/sh
# Reconcile every public service after an Orange Pi boot or an unexpected
# Docker restart.  Container restart policies cover the normal case; this is
# the second guard against boot ordering and network races.
set -eu

attempt=0
until docker info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Docker did not become ready within 120 seconds" >&2
    exit 1
  fi
  sleep 2
done

docker compose --project-directory /opt/llm-router --project-name llm-router up -d --no-build
docker compose --project-directory /opt/nas/app --project-name app up -d --no-build
