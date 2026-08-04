"""Request ID lifecycle tests."""

from uuid import UUID

from flask import Flask, g


def _register_probe(app: Flask) -> None:
    @app.get("/request-id")
    def request_id_probe() -> dict[str, str]:
        return {"request_id": g.request_id}


def test_valid_request_id_is_preserved(app: Flask) -> None:
    _register_probe(app)
    expected = "123e4567-e89b-12d3-a456-426614174000"

    response = app.test_client().get("/request-id", headers={"X-Request-ID": expected})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == expected
    assert response.get_json() == {"request_id": expected}


def test_invalid_request_id_is_replaced(app: Flask) -> None:
    _register_probe(app)

    response = app.test_client().get("/request-id", headers={"X-Request-ID": "not-valid"})

    generated = response.headers["X-Request-ID"]
    assert generated != "not-valid"
    assert str(UUID(generated)) == generated
