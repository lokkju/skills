#!/usr/bin/env bash
# Fails a pull request that changes a plugin's releasable files under a title
# that won't release it, unless the PR carries the no-release label.
#
# yeet releases a plugin for feat, fix and perf commits (bump_types in
# .yeet.yaml) and for any breaking commit (type!:) that touch the plugin's
# path outside its exclude_paths. This script reads the targets and types
# from .yeet.yaml, so the two agree. yeet's own release PRs (yeet/release-*)
# are skipped.
#
# Needs: GITHUB_EVENT_PATH (a pull_request event), GITHUB_REPOSITORY, GH_TOKEN
# with pull-requests: read, and yq (mikefarah) and jq on PATH.
set -euo pipefail

config=${YEET_CONFIG:-.yeet.yaml}
event=$GITHUB_EVENT_PATH

head_ref=$(jq -r '.pull_request.head.ref' "$event")
if [[ $head_ref == yeet/release-* ]]; then
  echo "yeet release PR ($head_ref); nothing to check."
  exit 0
fi

title=$(jq -r '.pull_request.title' "$event")
number=$(jq -r '.pull_request.number' "$event")

if jq -e '.pull_request.labels | any(.name == "no-release")' "$event" >/dev/null; then
  echo "Labeled no-release; nothing to check."
  exit 0
fi

# Conventional Commits header: type(scope)!: description
types=$(yq -o json '.bump_types' "$config" | jq -r '[.minor[]?, .patch[]?] | .[]')
releasable_title=false
if [[ $title =~ ^([A-Za-z]+)(\([^\)]*\))?(!)?:\  ]]; then
  type=$(tr '[:upper:]' '[:lower:]' <<<"${BASH_REMATCH[1]}")
  if [[ -n ${BASH_REMATCH[3]} ]] || grep -qxF "$type" <<<"$types"; then
    releasable_title=true
  fi
fi
if $releasable_title; then
  echo "Title \"$title\" releases; nothing to check."
  exit 0
fi

# Every path the PR touches, including the old side of a rename.
files=$(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$number/files" \
  --jq '.[] | .filename, (.previous_filename // empty)')

# under PATH PREFIX: PATH is PREFIX or inside it.
under() { [[ $1 == "$2" || $1 == "$2"/* ]]; }

hits=()
while IFS=$'\t' read -r target path excludes; do
  while IFS= read -r f; do
    if [[ -z $f ]] || ! under "$f" "$path"; then continue; fi
    excluded=false
    for x in $excludes; do
      if under "$f" "$x"; then excluded=true; break; fi
    done
    $excluded || hits+=("$target: $f")
  done <<<"$files"
done < <(yq -o json '.targets' "$config" |
  jq -r 'to_entries[] | select(.value.path) |
    [.key, .value.path, ((.value.exclude_paths // []) | join(" "))] | @tsv')

if ((${#hits[@]} == 0)); then
  echo "No releasable plugin files changed."
  exit 0
fi

{
  echo "This PR changes files that release a plugin:"
  printf '  %s\n' "${hits[@]}"
  echo
  echo "but its title, \"$title\", is not a releasable type (${types//$'\n'/, }, or any type with !)."
  echo "Retitle it (for example fix(<plugin>): ... or feat(<plugin>): ...) if users should get this change,"
  echo "or add the no-release label if they shouldn't."
} >&2
echo "::error title=Release check::Plugin files changed under a non-releasable title; retitle the PR as feat/fix/perf or add the no-release label."
exit 1
