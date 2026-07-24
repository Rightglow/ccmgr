from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_release_waits_for_reusable_cross_platform_test_workflow():
    release = (ROOT / ".github/workflows/release.yml").read_text()
    test = (ROOT / ".github/workflows/test.yml").read_text()

    assert "uses: ./.github/workflows/test.yml" in release
    assert "needs: test" in release
    assert "workflow_call:" in test
    assert "os: [ubuntu-latest, macos-latest]" in test


def test_tag_push_does_not_start_an_unrelated_duplicate_test_run():
    test = (ROOT / ".github/workflows/test.yml").read_text()

    push_section = test.split("pull_request:", 1)[0]
    assert "branches:" in push_section
    assert "- main" in push_section


def test_development_tag_creates_a_github_prerelease():
    release = (ROOT / ".github/workflows/release.yml").read_text()

    assert 'if [[ "$GITHUB_REF_NAME" == *.dev* ]]' in release
    assert "prerelease+=(--prerelease)" in release
    assert '"${prerelease[@]}"' in release
