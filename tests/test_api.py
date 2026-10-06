from fastapi.testclient import TestClient

from ranking_agent.api import app


def test_api_auth_idempotency_and_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("RANK_DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("RANK_API_TOKEN", "x" * 40)
    client = TestClient(app)
    payload = {"website": "example.com", "keyword": "demo"}
    assert client.post("/jobs", json=payload).status_code == 401
    headers = {"Authorization": "Bearer " + "x" * 40, "Idempotency-Key": "one"}
    first = client.post("/jobs", json=payload, headers=headers)
    assert first.status_code == 202
    assert client.post("/jobs", json=payload, headers=headers).json()["id"] == first.json()["id"]
    assert client.post("/jobs", json={**payload, "keyword": "changed"}, headers=headers).status_code == 409
    assert client.get("/evidence/not-a-run/.env", headers=headers).status_code == 404
