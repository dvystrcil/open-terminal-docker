"""
Regression tests for the /files/replace defensive check (homelab#107).

The check refuses a call where `target` equals the first non-empty line of
`replacement` — the signature of "model wants to insert a new section but
expressed it as a find-and-replace at an anchor it just invented." Without
this check, the call would get a generic 400 "Target string not found" and
the model typically follows up with "the file appears to have been truncated"
and goes into an unproductive re-read loop.
"""

from fastapi.testclient import TestClient

from open_terminal.main import app, get_filesystem, verify_api_key


# Stub out auth for all tests so we exercise the handler logic, not the
# auth path. The auth contract is tested elsewhere (or could be added in
# a separate file); these tests are about the /files/replace defensive check.
async def _noop_auth():
    return None


app.dependency_overrides[verify_api_key] = _noop_auth


class StubFS:
    """In-memory UserFS stand-in for the handler's three filesystem touchpoints."""

    def __init__(self, files: dict[str, str]):
        self._files = dict(files)

    def resolve_path(self, path: str) -> str:
        return path

    async def isfile(self, path: str) -> bool:
        return path in self._files

    async def read_text(self, path: str) -> str:
        return self._files[path]

    async def write(self, path: str, content: str) -> None:
        self._files[path] = content

    @property
    def files(self) -> dict[str, str]:
        return self._files


def _override_fs(files: dict[str, str]) -> StubFS:
    fs = StubFS(files)

    def _provide_fs():
        return fs

    app.dependency_overrides[get_filesystem] = _provide_fs
    return fs


def _client() -> TestClient:
    return TestClient(app)


def teardown_function(_):
    # Drop per-test fs override but keep the module-level auth no-op so
    # subsequent tests don't re-trip auth.
    app.dependency_overrides.pop(get_filesystem, None)


# --- The defensive check itself -------------------------------------------------


def test_target_equals_first_line_of_replacement_is_refused():
    """Smoking-gun pattern: target == replacement[0]. Refuse with hint, file untouched."""
    fs = _override_fs({"/x.md": "# Existing\n\nbody\n"})
    r = _client().post(
        "/files/replace",
        json={
            "path": "/x.md",
            "replacements": [
                {
                    "target": "### New Section",
                    "replacement": "### New Section\n\nnew body\n",
                }
            ],
        },
    )

    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "first line of the replacement" in detail
    # Error hint should still mention read_file (for anchor discovery).
    # The mention of the new insert/append ops is covered specifically by
    # test_insert_append.py::test_defensive_check_message_references_new_ops.
    assert "read_file" in detail
    # File must be unchanged.
    assert fs.files["/x.md"] == "# Existing\n\nbody\n"


def test_leading_blank_lines_in_replacement_dont_bypass_the_check():
    """First *non-empty* line is what counts, not the literal first line."""
    fs = _override_fs({"/x.md": "# Existing\n"})
    r = _client().post(
        "/files/replace",
        json={
            "path": "/x.md",
            "replacements": [
                {
                    "target": "## My Section",
                    "replacement": "\n\n## My Section\n\nbody\n",
                }
            ],
        },
    )
    assert r.status_code == 400, r.text
    assert "first line of the replacement" in r.json()["detail"]
    assert fs.files["/x.md"] == "# Existing\n"


def test_whitespace_only_target_does_not_trip_the_check():
    """Empty/whitespace target shouldn't trigger the defensive branch — let the
    existing 'Target string not found' path handle it normally."""
    _override_fs({"/x.md": "hello world\n"})
    r = _client().post(
        "/files/replace",
        json={
            "path": "/x.md",
            "replacements": [{"target": "   ", "replacement": "   replacement\n"}],
        },
    )
    # Whatever the existing handler returns is fine; only assert we didn't
    # short-circuit with the defensive check's message.
    assert "first line of the replacement" not in r.text


# --- Regressions: legitimate replaces must still work --------------------------


def test_target_in_middle_of_replacement_still_works():
    """The AC's explicit regression case. Anchor appears IN the replacement
    (e.g., wrap an existing string in surrounding context) — must succeed."""
    fs = _override_fs({"/x.md": "before\nFOO\nafter\n"})
    r = _client().post(
        "/files/replace",
        json={
            "path": "/x.md",
            "replacements": [
                {
                    "target": "FOO",
                    "replacement": "header\nFOO\ntrailing",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "before\nheader\nFOO\ntrailing\nafter\n"


def test_simple_replace_unaffected():
    """Baseline: normal find-and-replace where target != replacement[0] works."""
    fs = _override_fs({"/x.md": "alpha BETA gamma\n"})
    r = _client().post(
        "/files/replace",
        json={
            "path": "/x.md",
            "replacements": [{"target": "BETA", "replacement": "DELTA"}],
        },
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "alpha DELTA gamma\n"


def test_multiline_replacement_with_different_first_line_works():
    """Replace a heading with a multi-line block whose first line is different."""
    fs = _override_fs({"/x.md": "## Old Heading\n\nold body\n"})
    r = _client().post(
        "/files/replace",
        json={
            "path": "/x.md",
            "replacements": [
                {
                    "target": "## Old Heading\n\nold body\n",
                    "replacement": "## Renamed Heading\n\nrewritten body\n",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "## Renamed Heading\n\nrewritten body\n"
