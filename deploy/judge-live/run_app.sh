#!/bin/sh
set -eu

reader_password_file=${SCHEMABRIDGE_JUDGE_DB_READER_PASSWORD_FILE:-}

if [ ! -r "$reader_password_file" ]; then
  echo "the judge database reader secret file is unavailable" >&2
  exit 1
fi

IFS= read -r reader_password < "$reader_password_file"

case "$reader_password" in
  ''|*[!A-Za-z0-9]*)
    echo "the judge database reader secret must be 32-128 alphanumeric characters" >&2
    exit 1
    ;;
esac

if [ "${#reader_password}" -lt 32 ] || [ "${#reader_password}" -gt 128 ]; then
  echo "the judge database reader secret must be 32-128 alphanumeric characters" >&2
  exit 1
fi

export DATABASE_URL="postgresql://schemabridge_reader:${reader_password}@postgres:5432/schemabridge"
unset reader_password

exec "$@"
