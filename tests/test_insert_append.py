"""
Tests for the new insert/append operations (homelab#108):
    - POST /files/insert_after
    - POST /files/append_to_section
    - POST /files/append

These are companion operations to /files/replace, designed for ADDING content
rather than replacing existing content. The model picks the verb that matches
its intent: edit-in-place → replace, add-new → insert_after/append_*.
"""

from fastapi.testclient import TestClient

from open_terminal.main import app, get_filesystem, verify_api_key


async def _noop_auth():
    return None


app.dependency_overrides[verify_api_key] = _noop_auth


class StubFS:
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
    app.dependency_overrides.pop(get_filesystem, None)


# ============================================================================
# /files/insert_after
# ============================================================================


def test_insert_after_happy_path():
    """The canonical use case: insert a new section under an existing heading."""
    fs = _override_fs(
        {
            "/x.md": (
                "# Doc\n\n"
                "## 1. First\n"
                "body 1\n\n"
                "## 2. Second\n"
                "body 2\n"
            )
        }
    )
    r = _client().post(
        "/files/insert_after",
        json={
            "path": "/x.md",
            "anchor": "## 2. Second",
            "content": "## 3. Third\n\nbody 3\n",
        },
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == (
        "# Doc\n\n"
        "## 1. First\n"
        "body 1\n\n"
        "## 2. Second\n"
        "## 3. Third\n\nbody 3\n"
        "body 2\n"
    )


def test_insert_after_anchor_missing_returns_400():
    fs = _override_fs({"/x.md": "# Doc\n\ncontent\n"})
    r = _client().post(
        "/files/insert_after",
        json={"path": "/x.md", "anchor": "nonexistent heading", "content": "new\n"},
    )
    assert r.status_code == 400
    assert "Anchor not found" in r.json()["detail"]
    assert fs.files["/x.md"] == "# Doc\n\ncontent\n"  # unchanged


def test_insert_after_ambiguous_match_refused_by_default():
    fs = _override_fs({"/x.md": "## same\nfoo\n## same\nbar\n"})
    r = _client().post(
        "/files/insert_after",
        json={"path": "/x.md", "anchor": "## same", "content": "added\n"},
    )
    assert r.status_code == 400
    assert "allow_multiple is false" in r.json()["detail"]
    assert fs.files["/x.md"] == "## same\nfoo\n## same\nbar\n"


def test_insert_after_ambiguous_match_allowed_with_flag():
    fs = _override_fs({"/x.md": "## same\nfoo\n## same\nbar\n"})
    r = _client().post(
        "/files/insert_after",
        json={
            "path": "/x.md",
            "anchor": "## same",
            "content": "added\n",
            "allow_multiple": True,
        },
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "## same\nadded\nfoo\n## same\nadded\nbar\n"


def test_insert_after_adds_trailing_newline_to_content():
    """Caller passes content without trailing newline; handler should add one."""
    fs = _override_fs({"/x.md": "anchor line\nafter\n"})
    r = _client().post(
        "/files/insert_after",
        json={"path": "/x.md", "anchor": "anchor", "content": "no-newline"},
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "anchor line\nno-newline\nafter\n"


def test_insert_after_eof_anchor_without_trailing_newline():
    """Edge case: anchor is the last line of a file with no trailing newline."""
    fs = _override_fs({"/x.md": "first\nlast-line-no-newline"})
    r = _client().post(
        "/files/insert_after",
        json={"path": "/x.md", "anchor": "last-line", "content": "appended\n"},
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "first\nlast-line-no-newline\nappended\n"


def test_insert_after_file_not_found():
    _override_fs({})
    r = _client().post(
        "/files/insert_after",
        json={"path": "/missing.md", "anchor": "x", "content": "y"},
    )
    assert r.status_code == 404


# ============================================================================
# /files/append_to_section
# ============================================================================


SECTIONED_DOC = (
    "# Document\n"
    "\n"
    "## 1. First Section\n"
    "first body\n"
    "\n"
    "### 1.1 Sub-section\n"
    "sub body\n"
    "\n"
    "## 2. Second Section\n"
    "second body\n"
    "\n"
    "## 3. Third Section\n"
    "third body\n"
)


def test_append_to_section_inserts_before_next_same_depth_heading():
    fs = _override_fs({"/x.md": SECTIONED_DOC})
    r = _client().post(
        "/files/append_to_section",
        json={
            "path": "/x.md",
            "heading": "## 2. Second Section",
            "content": "new tail content\n",
        },
    )
    assert r.status_code == 200, r.text
    expected = (
        "# Document\n"
        "\n"
        "## 1. First Section\n"
        "first body\n"
        "\n"
        "### 1.1 Sub-section\n"
        "sub body\n"
        "\n"
        "## 2. Second Section\n"
        "second body\n"
        "\n"
        "new tail content\n"
        "## 3. Third Section\n"
        "third body\n"
    )
    assert fs.files["/x.md"] == expected


def test_append_to_section_skips_deeper_subheadings():
    """Adding to section 1 must skip past its h3 sub-section and stop at section 2."""
    fs = _override_fs({"/x.md": SECTIONED_DOC})
    r = _client().post(
        "/files/append_to_section",
        json={
            "path": "/x.md",
            "heading": "## 1. First Section",
            "content": "added to section 1\n",
        },
    )
    assert r.status_code == 200, r.text
    # The added content should land just before "## 2. Second Section",
    # i.e. AFTER the sub-section body.
    content = fs.files["/x.md"]
    assert content.index("added to section 1") < content.index("## 2.")
    assert content.index("### 1.1") < content.index("added to section 1")


def test_append_to_section_handles_eof():
    """Last section in file should append at EOF."""
    fs = _override_fs({"/x.md": SECTIONED_DOC})
    r = _client().post(
        "/files/append_to_section",
        json={
            "path": "/x.md",
            "heading": "## 3. Third Section",
            "content": "trailing\n",
        },
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"].endswith("third body\ntrailing\n")


def test_append_to_section_missing_heading_returns_400():
    fs = _override_fs({"/x.md": SECTIONED_DOC})
    r = _client().post(
        "/files/append_to_section",
        json={
            "path": "/x.md",
            "heading": "## 99. Nonexistent",
            "content": "x\n",
        },
    )
    assert r.status_code == 400
    assert "Markdown heading not found" in r.json()["detail"]
    assert fs.files["/x.md"] == SECTIONED_DOC


def test_append_to_section_hashtag_is_not_a_heading():
    """`#hashtag` (no space after #) must not match as a markdown heading."""
    fs = _override_fs({"/x.md": "#hashtag\nbody\n"})
    r = _client().post(
        "/files/append_to_section",
        json={"path": "/x.md", "heading": "#hashtag", "content": "x\n"},
    )
    assert r.status_code == 400  # no actual heading present


def test_append_to_section_file_not_found():
    _override_fs({})
    r = _client().post(
        "/files/append_to_section",
        json={"path": "/missing.md", "heading": "## x", "content": "y"},
    )
    assert r.status_code == 404


# ============================================================================
# /files/append
# ============================================================================


def test_append_basic():
    fs = _override_fs({"/x.md": "line one\n"})
    r = _client().post(
        "/files/append",
        json={"path": "/x.md", "content": "line two\n"},
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "line one\nline two\n"


def test_append_inserts_separating_newline_when_missing():
    """Existing file with no trailing newline shouldn't get content stuck on the last line."""
    fs = _override_fs({"/x.md": "line one"})
    r = _client().post(
        "/files/append",
        json={"path": "/x.md", "content": "line two\n"},
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "line one\nline two\n"


def test_append_adds_trailing_newline_to_content():
    fs = _override_fs({"/x.md": "existing\n"})
    r = _client().post(
        "/files/append",
        json={"path": "/x.md", "content": "no-newline"},
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "existing\nno-newline\n"


def test_append_empty_existing_file():
    fs = _override_fs({"/x.md": ""})
    r = _client().post(
        "/files/append",
        json={"path": "/x.md", "content": "first content\n"},
    )
    assert r.status_code == 200, r.text
    assert fs.files["/x.md"] == "first content\n"


def test_append_file_not_found():
    _override_fs({})
    r = _client().post(
        "/files/append",
        json={"path": "/missing.md", "content": "x"},
    )
    assert r.status_code == 404


# ============================================================================
# Updated defensive check (homelab#107) should now reference the new ops
# ============================================================================


def test_defensive_check_message_references_new_ops():
    """The replace defensive check's hint should now name insert_after / append_*."""
    fs = _override_fs({"/x.md": "existing\n"})
    r = _client().post(
        "/files/replace",
        json={
            "path": "/x.md",
            "replacements": [
                {"target": "## New", "replacement": "## New\n\nbody\n"}
            ],
        },
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    # Smoking-gun pattern still triggers
    assert "first line of the replacement" in detail
    # New op names mentioned (closes the loop with homelab#108)
    assert "insert_after" in detail
    assert "append_to_section" in detail
    assert "append_file_content" in detail
