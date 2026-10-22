#!/usr/bin/env bash
# Cut a release: bump the version, commit "Release X.Y.Z", tag vX.Y.Z, push.
# Usage: scripts/release.sh 0.6.0
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

version="${1:-}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: scripts/release.sh X.Y.Z" >&2
  exit 2
fi
tag="v$version"
init=src/ralph_kit/__init__.py

current="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$init")"
if [[ -z "$current" ]]; then
  echo "cannot read __version__ from $init" >&2
  exit 1
fi
if [[ "$current" == "$version" ]]; then
  echo "already at $version" >&2
  exit 1
fi

if [[ "$(git branch --show-current)" != main ]]; then
  echo "releases are cut from main" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree is not clean" >&2
  exit 1
fi
git fetch --quiet origin main --tags
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
  echo "main is not in sync with origin/main" >&2
  exit 1
fi
if git rev-parse --quiet --verify "refs/tags/$tag" >/dev/null; then
  echo "tag $tag already exists" >&2
  exit 1
fi

echo "Running lint and tests..."
uvx ruff==0.17.1 check src tests
uv run python -m unittest discover -s tests

sed -i.bak "s/^__version__ = \"$current\"$/__version__ = \"$version\"/" "$init"
sed -i.bak "s/ralph-kit@v$current/ralph-kit@$tag/" README.md
rm -f "$init.bak" README.md.bak

git diff
read -r -p "Release $current -> $version and push to origin? [y/N] " answer
if [[ "$answer" != [yY] ]]; then
  git checkout -- "$init" README.md
  echo "aborted, changes reverted"
  exit 1
fi

git commit --quiet -am "Release $version"
git tag "$tag"
git push origin main
git push origin "$tag"
echo "Released $tag"
