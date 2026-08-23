#!/usr/bin/env bash
# GitHub mention checks: a state matrix, then real git against throwaway remotes.
set -euo pipefail

repository="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repository"

if [[ -x "$repository/.venv/bin/python" ]]; then
  python=("$repository/.venv/bin/python")
else
  python=(uv run --project "$repository" python)
fi

"${python[@]}" "$repository/tests/github_mention_matrix.py"

ephemeral="$(mktemp -d)"
trap 'rm -rf "$ephemeral"' EXIT
origin="$ephemeral/origin.git"
work="$ephemeral/work"

git -c init.defaultBranch=main init --bare "$origin"
git -c init.defaultBranch=main clone "$origin" "$work"
git -C "$work" config user.name test
git -C "$work" config user.email test@test
printf 'base\n' > "$work/README"
printf '.github/langmesh/\n' > "$work/.gitignore"
mkdir -p "$work/.github/workflows"
printf 'name: ci\n' > "$work/.github/workflows/ci.yml"
git -C "$work" add README .gitignore .github/workflows/ci.yml
git -C "$work" commit -m init
git -C "$work" push -u origin main
git -C "$work" checkout -b feature
printf 'feat\n' > "$work/feat.txt"
git -C "$work" add feat.txt
git -C "$work" commit -m feat
git -C "$work" push -u origin feature
git -C "$work" checkout main

export LANGMESH_WORKSPACE="$work"

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

"${python[@]}" - <<'PY'
import os
from pathlib import Path

from langmesh.github.mention import tree_is_dirty

work = Path(os.environ["LANGMESH_WORKSPACE"])
(work / ".github" / "langmesh").mkdir(parents=True)
(work / ".github" / "langmesh" / "session.sqlite").write_text("ckpt\n")
if tree_is_dirty(work):
    raise SystemExit("state directory alone must not look dirty")
(work / ".github" / "workflows" / "ci.yml").write_text("name: dirty\n")
if not tree_is_dirty(work):
    raise SystemExit("a workflow edit under .github must still look dirty")
PY
git -C "$work" reset -q
git -C "$work" checkout -- .github/workflows/ci.yml

printf 'edit\n' > "$work/README"
"${python[@]}" - <<'PY'
import os
from pathlib import Path

from langmesh.github.mention import tree_is_dirty

if not tree_is_dirty(Path(os.environ["LANGMESH_WORKSPACE"])):
    raise SystemExit("a tracked edit must look dirty")
PY
git -C "$work" reset -q
git -C "$work" checkout -- README

# Token rides in git -c and must never land in the checkout.
"${python[@]}" - <<'PY'
import os
from pathlib import Path

from langmesh.github.mention import Mention, prepare_tree

work = Path(os.environ["LANGMESH_WORKSPACE"])
mention = Mention(
    body="@langmesh",
    number=12,
    kind="pull",
    title="Flaky test",
    html_url="https://example.test/12",
    user="owner",
    association="OWNER",
    default_branch="main",
    repository="ghovax/langmesh",
    head_ref="feature",
    head_repository="ghovax/langmesh",
)
prepare_tree(mention, work, token="secret-token")
PY
[[ "$(git -C "$work" rev-parse --abbrev-ref HEAD)" == feature ]] || fail "prepare_tree did not check out the pull head"
if git -C "$work" config --get http.extraheader >/dev/null; then
  fail "token extraheader was written into git config"
fi
if grep -R --exclude-dir=.git -q "secret-token" "$work"; then
  fail "token leaked into the checkout"
fi

mkdir -p "$work/.github/langmesh"
printf 'ckpt\n' > "$work/.github/langmesh/session.sqlite"
printf 'done\n' >> "$work/feat.txt"
printf 'name: noted\n' > "$work/.github/workflows/ci.yml"
"${python[@]}" - <<'PY'
import os
from pathlib import Path

from langmesh.github.mention import Mention, publish_changes

work = Path(os.environ["LANGMESH_WORKSPACE"])
mention = Mention(
    body="@langmesh",
    number=12,
    kind="pull",
    title="Flaky test",
    html_url="https://example.test/12",
    user="owner",
    association="OWNER",
    default_branch="main",
    repository="ghovax/langmesh",
    head_ref="feature",
    head_repository="ghovax/langmesh",
)
if publish_changes(mention, work, token="") != "":
    raise SystemExit("a pull mention must not create a pull request")
PY
[[ "$(git -C "$work" log -1 --format=%s)" == "langmesh: Flaky test" ]] || fail "commit message was not inline"
if git -C "$work" ls-tree -r --name-only HEAD | grep -q '^\.github/langmesh/'; then
  fail "session state was committed"
fi
git -C "$work" show HEAD:.github/workflows/ci.yml | grep -q 'name: noted' || fail "workflow edit under .github was not committed"
git -C "$origin" rev-parse --verify feature >/dev/null

git -C "$work" checkout main
printf 'oops\n' >> "$work/README"
"${python[@]}" - <<'PY'
import os
from pathlib import Path

from langmesh.github.mention import Mention, publish_changes

work = Path(os.environ["LANGMESH_WORKSPACE"])
mention = Mention(
    body="@langmesh",
    number=1,
    kind="issue",
    title="x",
    html_url="https://example.test/1",
    user="owner",
    association="OWNER",
    default_branch="main",
    repository="ghovax/langmesh",
)
try:
    publish_changes(mention, work, token="")
except RuntimeError as error:
    if "protected branch" not in str(error):
        raise SystemExit(error)
else:
    raise SystemExit("publish_changes must refuse main")
PY
git -C "$work" checkout -- README

mkdir -p "$work/.github/langmesh"
printf 'langmesh/flaky-test-ab12\n' > "$work/.github/langmesh/branch"
git -C "$work" branch langmesh/flaky-test-ab12 origin/main
git -C "$work" push origin langmesh/flaky-test-ab12
git -C "$work" checkout main
git -C "$work" branch -D langmesh/flaky-test-ab12
"${python[@]}" - <<'PY'
import os
from pathlib import Path

from langmesh.github.mention import Mention, current_branch, prepare_tree

work = Path(os.environ["LANGMESH_WORKSPACE"])
mention = Mention(
    body="@langmesh",
    number=12,
    kind="issue",
    title="Flaky test",
    html_url="https://example.test/12",
    user="owner",
    association="OWNER",
    default_branch="main",
    repository="ghovax/langmesh",
)
prepare_tree(mention, work, token="")
if current_branch(work) != "langmesh/flaky-test-ab12":
    raise SystemExit(f"expected remembered branch, got {current_branch(work)!r}")
PY

git -C "$work" checkout main
git -C "$work" branch -D langmesh/flaky-test-ab12 2>/dev/null || true
rm -f "$work/.github/langmesh/branch"
"${python[@]}" - <<'PY'
import os
from pathlib import Path

from langmesh.github.mention import Mention, current_branch, prepare_tree

work = Path(os.environ["LANGMESH_WORKSPACE"])
mention = Mention(
    body="@langmesh",
    number=99,
    kind="issue",
    title="New",
    html_url="https://example.test/99",
    user="owner",
    association="OWNER",
    default_branch="main",
    repository="ghovax/langmesh",
)
first = prepare_tree(mention, work, token="")
if first.resumed or first.branch != "main":
    raise SystemExit(f"new issue should start on the default branch, got {first!r}")
if current_branch(work) != "main":
    raise SystemExit(f"checkout was {current_branch(work)!r}")
if (work / ".github/langmesh/branch").exists():
    raise SystemExit("new issue must not pre-create a branch name")
second = prepare_tree(mention, work, token="")
if second.branch != "main" or second.resumed:
    raise SystemExit(f"follow-up before a draft should stay on main, got {second!r}")
PY

echo "github mention ephemeral git: ok"
