from __future__ import annotations

from core.api.services.openai_responses import extract_output_text


def test_extract_output_text_prefers_output_text() -> None:
    assert (
        extract_output_text({"output_text": "  APPROVE because safe.  "})
        == "APPROVE because safe."
    )


def test_extract_output_text_falls_back_to_nested_output() -> None:
    payload = {
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": "0 9 * * 1"},
                ]
            }
        ]
    }

    assert extract_output_text(payload) == "0 9 * * 1"
