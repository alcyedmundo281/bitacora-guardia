"""Pruebas del servidor Webhook de WhatsApp."""

from fastapi.testclient import TestClient
from bitacora.webhook_server import app

client = TestClient(app)


def test_webhook_health() -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_webhook_consult_night_summary() -> None:
    res = client.post("/whatsapp/webhook", json={"message": "!guardia"})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "CHECKLIST DE LA VISITA NOCTURNA" in data["reply"]


def test_webhook_receive_result() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "6A resultado: Lactato 1.4 mmol/L y gases normales"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "6A" in data["reply"]
