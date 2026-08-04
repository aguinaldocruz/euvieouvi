"""Expected application error tests."""

from typing import NoReturn

from flask import Flask

from euvieouvi.errors import AppError


def test_app_error_uses_stable_safe_payload(app: Flask) -> None:
    @app.get("/expected-error")
    def expected_error() -> NoReturn:
        raise AppError(code="example_error", message="Safe message.", status=409)

    response = app.test_client().get("/expected-error")

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"]["code"] == "example_error"
    assert payload["error"]["message"] == "Safe message."
    assert payload["error"]["status"] == 409
    assert payload["error"]["request_id"] == response.headers["X-Request-ID"]
    assert payload["error"]["details"] == []
