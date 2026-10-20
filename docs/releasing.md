# Releasing

A release is a version bump on `main`, a tag on that commit and one approval.
The bump is the one change that does not go through a pull request: the
maintainer pushes it to `main` directly.
The tag starts `.github/workflows/publish.yml`, which runs a consumer's tests
against the wheel, uploads to TestPyPI on its own, tests the release as
TestPyPI serves it, and uploads to PyPI once the release has soaked on a
live installation and the owner approves. Both indexes trust this
repository's workflow directly (trusted publishing), so no token is stored
anywhere.

## 1. The release commit

The version has one source, the `__version__` literal in
`mower_sdk/__init__.py`; `pyproject.toml` reads it from there. Versions follow
PEP 440. Until 0.2.0 is final they are pre-releases, `0.2.0aN`.

One commit, `chore(release): bump version to <version>`, changes two files:

- `mower_sdk/__init__.py`: the new `__version__`.
- `CHANGELOG.md`: a `## [<version>] - <date>` heading goes directly under
  `## [Unreleased]`, so the unreleased entries become that version's, and the
  link list at the foot of the file gets the new version's compare link and
  an updated `[Unreleased]` link.

The [commit-message rules](development.md#commit-messages) require this
shape for any change to `__version__`.

Bump the version before building anything to test. A wheel records the version
it was built with, so a wheel built before the bump is not the one that will
be released.

## 2. Test what will be released

```bash
nox
```

That runs every [session](development.md#the-checks), the `wheel` session
among them, which tests the built wheel as an installed package. To try the
release in a consumer first, build it and install the file:

```bash
pip install build
python -m build
pip install dist/navimow_sdk_community-<version>-py3-none-any.whl
```

## 3. Tag

An annotated tag named `v<version>` on the release commit, pushed on its own:

```bash
git tag -a v<version> -m "navimow-sdk-community <version>"
git push origin v<version>
```

If the tag lands on a later day than the changelog says, correct the date in
a second `chore(release)` commit first and tag that one.

## 4. What the workflow does

Every `v*` tag runs five jobs, each stopping the run when it fails:

1. **build** checks that the tag is `v` plus `mower_sdk.__version__` and stops
   if it is not, builds the sdist and the wheel once, and runs
   `twine check --strict` on them.
2. **compat** runs a consumer's tests against the wheel. The consumer is
   named by two repository variables, set under the repository's settings:
   `CONSUMER_REPOSITORY`, a public GitHub repository as `owner/name`, and
   `CONSUMER_REF`, the branch, tag or commit to check out. The job checks it
   out and runs its `scripts/test-against-sdk.sh --wheel <path>` from the
   consumer's own directory, with the absolute path of the wheel; what the
   script tests and how is the consumer's business, and its exit status is
   the verdict. So a release that reaches TestPyPI has passed the consumer's
   tests. With either variable unset the job passes, and the run's summary
   says that no consumer's tests ran.
3. **testpypi** uploads the files to TestPyPI, unattended.
4. **testpypi-verify** checks the release as TestPyPI serves it, on the oldest
   and the newest supported Python. `tools/check_index.py` reads TestPyPI's
   record of the version, asking again while the index does not know it yet,
   and compares the files it serves with the build's by name and sha256. The
   job then has pip download the wheel from TestPyPI's simple index, checks
   that it is byte for byte the built one, installs it with the test
   dependencies into a fresh environment, and runs the tag's `tests/` from
   another directory, so the installed package is what gets imported, plus
   `tools/check_wheel.py metadata`. This is the fresh-environment check that
   used to be done by hand.
5. **pypi** waits. It runs in the `pypi` environment, whose required-reviewer
   rule holds it until the owner approves the deployment. Its first step then
   reads the commit status `navimow-alpha/soak` on the tag's commit and
   refuses the upload unless it is `success`: whoever deploys the release to
   a live installation reports the outcome of its soak there as that status.
   Once it is, the job uploads the same files to PyPI.

## 5. Wait for the soak, then approve

Two things have to hold before the approval: `testpypi-verify` has passed,
and the status `navimow-alpha/soak` on the tag's commit is `success`. The
run's page shows the first; the commit's status list on GitHub shows the
second, as does

```bash
gh api repos/geordiekorper/navimow-sdk-community/commits/v<version>/status --jq '.statuses[] | select(.context == "navimow-alpha/soak") | .state'
```

Then approve the `pypi` deployment on the run's page. An approval given
before the status is green fails at the job's first step; once it is green,
re-run the job and approve again.

The owner can waive the soak by posting the status by hand. A status carries
its description, so the waiver is on the commit for anyone to read:

```bash
gh api --method POST "repos/geordiekorper/navimow-sdk-community/statuses/$(git rev-parse 'v<version>^{commit}')" -f state=success -f context=navimow-alpha/soak -f description='soak waived by the owner'
```

The `pypi` job uploads the files the `build` job made, the ones TestPyPI
received and served, and nothing is checked from PyPI afterwards: the
dependencies that `testpypi-verify` installed beside the wheel came from
PyPI already, so a second run from there would repeat it.

## When something is wrong

A version number can be used once on each index: a file that was uploaded
cannot be replaced, even after deleting the release. Fix the problem on
`main` and release the next number. A release that should not be installed
can be yanked on PyPI, which hides it from new installs without breaking
pins.

`tools/tests/test_publish_workflow.py` pins the shape of the workflow
described here.
