#!/usr/bin/env bash
# PI session: use hh1985 GitHub account
export GH_TOKEN="$(gh auth token --user hh1985)"
export GIT_AUTHOR_NAME="hh1985"
export GIT_COMMITTER_NAME="hh1985"
export GIT_AUTHOR_EMAIL="hh1985@users.noreply.github.com"
export GIT_COMMITTER_EMAIL="hh1985@users.noreply.github.com"
exec "$@"
