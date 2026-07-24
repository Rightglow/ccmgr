from pathlib import Path

import pytest

from tools.release_notes import render_release_notes


CHANGELOG = """\
# Changelog

## [Unreleased]

- In progress.

## [0.2.3] - 2026-07-22

### Added

- Exact release notes.

## [0.2.30] - 2026-08-01

- A different release.

[Unreleased]: https://example.test/compare/v0.2.3...HEAD
[0.2.3]: https://example.test/compare/v0.2.2...v0.2.3
[0.2.30]: https://example.test/compare/v0.2.3...v0.2.30
"""


def test_renders_exact_release_section_and_comparison_link():
    assert render_release_notes(CHANGELOG, "v0.2.3") == (
        "### Added\n\n"
        "- Exact release notes.\n\n"
        "**Full Changelog**: "
        "https://example.test/compare/v0.2.2...v0.2.3\n"
    )


def test_renders_development_release_section():
    changelog = (
        "## [0.2.11.dev202607240] - 2026-07-24\n\n"
        "- Development release.\n\n"
        "[0.2.11.dev202607240]: "
        "https://example.test/compare/v0.2.10...v0.2.11.dev202607240\n"
    )

    assert render_release_notes(
        changelog, "v0.2.11.dev202607240"
    ) == (
        "- Development release.\n\n"
        "**Full Changelog**: "
        "https://example.test/compare/"
        "v0.2.10...v0.2.11.dev202607240\n"
    )


def test_last_section_does_not_include_changelog_link_definitions():
    notes = render_release_notes(CHANGELOG, "0.2.30")

    assert notes.startswith("- A different release.\n")
    assert "[Unreleased]:" not in notes
    assert notes.count("**Full Changelog**:") == 1


@pytest.mark.parametrize(
    "version",
    ["0.2.4", "Unreleased", "v0.2", "0.2.3-dev1", "0.2.3.dev"],
)
def test_rejects_missing_or_invalid_release(version):
    with pytest.raises(ValueError):
        render_release_notes(CHANGELOG, version)


def test_rejects_empty_release_section():
    changelog = "## [0.2.3] - today\n\n## [0.2.2] - yesterday\n\n- Old.\n"

    with pytest.raises(ValueError, match="is empty"):
        render_release_notes(changelog, "0.2.3")


def test_does_not_repeat_a_release_self_link_as_full_changelog():
    changelog = (
        "## [0.1.1] - today\n\n- First release.\n\n"
        "[0.1.1]: https://example.test/releases/tag/v0.1.1\n"
    )

    assert render_release_notes(changelog, "0.1.1") == "- First release.\n"


def test_cli_writes_requested_output(tmp_path, monkeypatch):
    from tools import release_notes

    changelog = tmp_path / "CHANGELOG.md"
    output = tmp_path / "notes.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.chdir(Path(tmp_path))

    assert release_notes.main(["0.2.3", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == render_release_notes(
        CHANGELOG, "0.2.3")
