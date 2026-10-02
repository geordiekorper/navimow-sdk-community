# Releasing

A release is a version bump on `main`, a tag on that commit and one approval.
The tag starts `.github/workflows/publish.yml`, which uploads to TestPyPI on
its own and to PyPI once the owner approves. Both indexes trust this
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

Every `v*` tag runs three jobs:

1. **build** checks that the tag is `v` plus `mower_sdk.__version__` and stops
   if it is not, builds the sdist and the wheel once, and runs
   `twine check --strict` on them.
2. **testpypi** uploads those files to TestPyPI, unattended.
3. **pypi** waits. It runs in the `pypi` environment, whose required-reviewer
   rule holds it until the owner approves the deployment, and then uploads the
   same files to PyPI. A failed TestPyPI upload stops the run before this job.

## 5. Check, approve, check

While the `pypi` job waits, install the release from TestPyPI into a fresh
environment. The dependencies come from PyPI, hence the second index:

```bash
python -m venv /tmp/release-check
/tmp/release-check/bin/pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple navimow-sdk-community==<version> pytest
```

Run the tag's tests against it from a directory that is not the checkout, so
the installed package is what gets imported:

```bash
git archive v<version> tests | tar -x -C /tmp/release-check
cd /tmp/release-check && bin/python -m pytest tests
```

Then approve the `pypi` deployment on the workflow run's page. Once it has
finished, repeat the check from PyPI in a second fresh environment, so that
nothing installed from TestPyPI is reused, and run the same tests with it:

```bash
python -m venv /tmp/release-check-pypi
/tmp/release-check-pypi/bin/pip install navimow-sdk-community==<version> pytest
cd /tmp/release-check && /tmp/release-check-pypi/bin/python -m pytest tests
```

## When something is wrong

A version number can be used once on each index: a file that was uploaded
cannot be replaced, even after deleting the release. Fix the problem on
`main` and release the next number. A release that should not be installed
can be yanked on PyPI, which hides it from new installs without breaking
pins.

`tools/tests/test_publish_workflow.py` pins the shape of the workflow
described here.
