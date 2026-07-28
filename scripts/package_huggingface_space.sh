#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  printf '%s\n' 'Usage: scripts/package_huggingface_space.sh RELEASE_REF OUTPUT_DIRECTORY' >&2
  exit 2
fi

release_ref="$1"
output_directory="$2"

if [[ -e "$output_directory" ]]; then
  printf 'Output path already exists: %s\n' "$output_directory" >&2
  exit 2
fi

release_commit="$(git rev-parse --verify "${release_ref}^{commit}")"
required_paths=(
  Dockerfile
  deploy/huggingface/README.md
  demo/hosted/north_star_execution.json
  src/schemabridge/adapters/demo/recorded_execution.py
)
for required_path in "${required_paths[@]}"; do
  if ! git cat-file -e "${release_commit}:${required_path}" 2>/dev/null; then
    printf 'Release commit lacks required M17 path: %s\n' "$required_path" >&2
    exit 2
  fi
done
mkdir -p "$output_directory"
git archive "$release_commit" | tar -x -C "$output_directory"
cp "$output_directory/deploy/huggingface/README.md" "$output_directory/README.md"
printf '%s\n' "$release_commit" > "$output_directory/RELEASE_COMMIT"
printf 'Packaged Hugging Face Space release %s at %s\n' "$release_commit" "$output_directory"
