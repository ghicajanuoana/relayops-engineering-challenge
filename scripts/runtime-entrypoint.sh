#!/bin/sh
set -eu

if [ -n "${RELAYOPS_DATABASE_PASSWORD_FILE:-}" ]; then
    if [ -z "${PGPASSWORD:-}" ]; then
        PGPASSWORD=$(cat "$RELAYOPS_DATABASE_PASSWORD_FILE")
    fi
    test -n "$PGPASSWORD"
    export PGPASSWORD
    if [ -z "${POSTGRES_PASSWORD:-}" ]; then
        POSTGRES_PASSWORD=$PGPASSWORD
        export POSTGRES_PASSWORD
    fi
fi

if [ -n "${RELAYOPS_WEBHOOK_SECRET_FILE:-}" ]; then
    if [ -z "${WEBHOOK_SECRET:-}" ]; then
        WEBHOOK_SECRET=$(cat "$RELAYOPS_WEBHOOK_SECRET_FILE")
    fi
    test -n "$WEBHOOK_SECRET"
    export WEBHOOK_SECRET
fi

# Preserve PID 1 signal delivery, including the worker's SIGTERM handler.
exec "$@"
