import httpx
import pytest

pytestmark = pytest.mark.anyio

DOC = {"title": "FAQ", "content": "## Pagos\n\nAceptamos tarjeta."}


def auth(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key}


@pytest.mark.parametrize("headers", [{}, {"X-Api-Key": "sk_inventada"}])
async def test_requires_valid_api_key(client: httpx.AsyncClient, demo_app, headers) -> None:
    response = await client.put("/admin/documents/faq", json=DOC, headers=headers)

    assert response.status_code == 401


async def test_put_creates_then_is_idempotent(client: httpx.AsyncClient, demo_app) -> None:
    _, api_key = demo_app

    created = await client.put("/admin/documents/faq", json=DOC, headers=auth(api_key))
    repeated = await client.put("/admin/documents/faq", json=DOC, headers=auth(api_key))

    assert created.status_code == 200
    assert created.json() == {
        "external_id": "faq",
        "status": "created",
        "chunks": 1,
        "pending_embeddings": 1,
    }
    assert repeated.json()["status"] == "unchanged"


async def test_put_validates_body(client: httpx.AsyncClient, demo_app) -> None:
    response = await client.put(
        "/admin/documents/faq", json={"title": "", "content": "x"}, headers=auth(demo_app[1])
    )

    assert response.status_code == 422


async def test_delete(client: httpx.AsyncClient, demo_app) -> None:
    _, api_key = demo_app
    await client.put("/admin/documents/faq", json=DOC, headers=auth(api_key))

    first = await client.delete("/admin/documents/faq", headers=auth(api_key))
    second = await client.delete("/admin/documents/faq", headers=auth(api_key))

    assert (first.status_code, second.status_code) == (204, 404)


async def test_apps_cannot_touch_each_others_documents(
    client: httpx.AsyncClient, demo_app, other_app
) -> None:
    await client.put("/admin/documents/faq", json=DOC, headers=auth(demo_app[1]))

    response = await client.delete("/admin/documents/faq", headers=auth(other_app[1]))

    assert response.status_code == 404
