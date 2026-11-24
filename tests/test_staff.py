import httpx

from servicepilot.main import create_app


async def test_staff_validation_does_not_echo_password_input():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as api:
        response = await api.post("/staff/sessions", json={
            "dealership": "demo", "username": "advisor", "password": ["synthetic-private-value"],
        })

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "password"]
    assert "synthetic-private-value" not in response.text
