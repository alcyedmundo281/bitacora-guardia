"""Pruebas del servidor Webhook de WhatsApp y Robotina."""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from bitacora.webhook_server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def preserve_bitacora_data():
    """Guarda copia del archivo de bitácora y lo restaura tras las pruebas."""
    path = Path("data/bitacora_2026_11_10.json")
    original_content = path.read_bytes() if path.exists() else None
    yield
    if original_content and path.exists():
        path.write_bytes(original_content)


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


def test_robotina_natural_language_result() -> None:
    """Prueba que Robotina entienda 'Robotina la gasometria de 6a es pf 6.9'."""
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina la gasometria de 6a es pf 6.9"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["tipo_accion"] == "RESULTADO_REGISTRADO"
    assert "6A" in data["reply"]
    assert "pf 6.9" in data["reply"]


def test_robotina_pendientes() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina pendientes"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "CHECKLIST DE LA VISITA NOCTURNA" in data["reply"]


def test_robotina_consulta_cama() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina como esta la 6a"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "6A" in data["reply"]
    assert "RODRIGUEZ" in data["reply"]


def test_robotina_ayuda() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina ayuda"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "Robotina" in data["reply"]


def test_robotina_generar_pdf() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina genera el pdf"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["tipo_accion"] == "GENERAR_PDF"
    assert "descargar/bitacora.pdf" in data["reply"]


def test_endpoint_descargar_pdf() -> None:
    res = client.get("/descargar/bitacora.pdf")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"

