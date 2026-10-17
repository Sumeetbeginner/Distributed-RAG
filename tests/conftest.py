import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


@pytest.fixture
def system(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/test.db",
        admin_key="a" * 32,
        rate_limit=10000,
        lease_seconds=3,
        chunk_words=50,
        chunk_overlap=10,
    )
    app = create_app(settings)
    app.state.runtime.initialize()
    with TestClient(app) as client:

        def tenant(name):
            response = client.post(
                "/api/admin/tenants", json={"name": name}, headers={"X-Admin-Key": "a" * 32}
            )
            assert response.status_code == 201
            data = response.json()
            return data["tenant_id"], {"Authorization": "Bearer " + data["api_key"]}

        a, auth_a = tenant("Alpha")
        b, auth_b = tenant("Beta")
        yield client, app.state.runtime, a, auth_a, b, auth_b


def ingest(
    client, headers, external_id="runbook", content="Worker recovery uses leases and fencing tokens.", **extra
):
    return client.post(
        "/api/documents",
        headers=headers,
        json={"external_id": external_id, "title": "Recovery guide", "content": content, **extra},
    )
