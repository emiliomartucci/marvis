from core.wizard.state import FirstProjectPayload
from core.wizard.validation import validate_first_project


def test_first_project_accepts_single_character_slug() -> None:
    assert (
        validate_first_project(FirstProjectPayload(name="x", slug="a"))
        == []
    )


def test_first_project_rejects_slug_with_invalid_start() -> None:
    errors = validate_first_project(
        FirstProjectPayload(name="x", slug="-bad")
    )

    assert [error.field for error in errors] == ["slug"]
