from unittest.mock import Mock

from fastapi.testclient import TestClient

from servicepilot.main import create_app


def test_health_and_readiness_with_postgresql(database_settings, migrated_database):
    with TestClient(create_app(database_settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_health_stays_up_when_database_is_unavailable(database_settings, monkeypatch):
    app = create_app(database_settings)
    with TestClient(app) as client:
        monkeypatch.setattr(type(app.state.engine), "connect", Mock(side_effect=OSError("offline")))
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert client.get("/health").status_code == 200
