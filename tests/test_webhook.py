"""Pruebas del servidor Webhook de WhatsApp y Robotina."""

import shutil
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from bitacora.webhook_server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def preserve_bitacora_data():
    """Guarda copia del archivo de bitácora y de data/camas/ y los restaura tras las pruebas."""
    path = Path("data/bitacora_2026_11_10.json")
    camas_dir = Path("data/camas")
    meta_file = Path("data/guardia_meta.json")

    original_content = path.read_bytes() if path.exists() else None
    original_meta = meta_file.read_bytes() if meta_file.exists() else None
    camas_backup: dict[str, bytes] = {}
    if camas_dir.exists():
        for f in camas_dir.glob("*.json"):
            camas_backup[f.name] = f.read_bytes()

    yield

    if original_content and path.exists():
        path.write_bytes(original_content)
    if original_meta:
        meta_file.write_bytes(original_meta)
    elif meta_file.exists():
        meta_file.unlink()
    if camas_dir.exists():
        shutil.rmtree(camas_dir)
    if camas_backup:
        camas_dir.mkdir(parents=True, exist_ok=True)
        for name, data in camas_backup.items():
            (camas_dir / name).write_bytes(data)


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


def test_robotina_crear_recordatorio() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "robotina recuerdanos a las 7 el sodio del sr rodriguez"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["tipo_accion"] == "CREAR_RECORDATORIO"
    assert "07:00" in data["reply"]
    assert "RODRIGUEZ" in data["reply"]
    assert "6A" in data["reply"]


def test_debug_groq_endpoint() -> None:
    res = client.get("/debug/groq")
    assert res.status_code == 200
    assert "status" in res.json()


def test_robotina_screenshot_recordatorio() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina recuerdanos reportar sodio para 8a a las 7pm"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["tipo_accion"] == "CREAR_RECORDATORIO"
    assert "19:00" in data["reply"]
    assert "8A" in data["reply"]


def test_robotina_screenshot_examenes_rodriguez() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina Que examenes últimos de Rodríguez de periférico"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["tipo_accion"] == "CONSULTA_CAMA"
    assert "RODRIGUEZ" in data["reply"]
    assert "6A" in data["reply"]


def test_bed_store_discharge_and_admission() -> None:
    from bitacora.bed_store import (
        actualizar_evolucion_cama,
        egresar_paciente_cama,
        get_cama,
        ingresar_paciente_cama,
    )

    # 1. Confirmar que 6A existe inicialmente
    assert get_cama("6A") is not None

    # 2. Sacar/egresar al paciente de 6A
    res_eg = egresar_paciente_cama("6A", motivo="Alta médica")
    assert res_eg["ok"] is True
    assert get_cama("6A") is None  # Ya no ocupa esa cama

    # 3. Ingresar un paciente nuevo en 6A y agregar evolución
    res_ing = ingresar_paciente_cama("6A", "MARIA LOPEZ", edad="45", diagnostico="Sepsis urinaria")
    assert res_ing["ok"] is True
    res_ev = actualizar_evolucion_cama("6A", "Afebril, estable")
    assert res_ev["ok"] is True

    pac_nuevo = get_cama("6A")
    assert pac_nuevo is not None
    assert "MARIA LOPEZ" in pac_nuevo.nombre_completo
    assert any("Afebril" in o for o in pac_nuevo.observaciones)


def test_api_camas_endpoint() -> None:
    res = client.get("/api/camas")
    assert res.status_code == 200
    data = res.json()
    assert data["total_ocupadas"] > 0
    assert any(c["cama"] == "6A" for c in data["camas"])


def test_daily_pdf_scheduler_ecuador_timezone(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timezone
    from bitacora import reminders

    state_file = tmp_path / "auto_pdf_state.json"
    monkeypatch.setattr(reminders, "AUTO_PDF_STATE_FILE", state_file)

    # 09:59 UTC == 04:59 Ecuador -> aún no debe publicar
    dt_0459_ec = datetime(2026, 11, 13, 9, 59, tzinfo=timezone.utc).astimezone(reminders.ECUADOR_TZ)
    assert reminders.should_publish_daily_pdf(ahora=dt_0459_ec) is False

    # 10:00 UTC == 05:00 Ecuador -> debe publicar
    dt_0500_ec = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc).astimezone(reminders.ECUADOR_TZ)
    assert reminders.should_publish_daily_pdf(ahora=dt_0500_ec) is True

    # Tras marcar como publicado hoy, no debe volver a disparar el mismo día
    reminders.mark_daily_pdf_published(ahora=dt_0500_ec)
    assert reminders.should_publish_daily_pdf(ahora=dt_0500_ec) is False

    # Al día siguiente a las 05:00 Ecuador sí debe volver a publicar
    dt_next_day = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc).astimezone(reminders.ECUADOR_TZ)
    assert reminders.should_publish_daily_pdf(ahora=dt_next_day) is True





