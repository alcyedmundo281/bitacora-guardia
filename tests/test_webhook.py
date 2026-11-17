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
    assert "RODRIGUEZ" in data["reply"].replace("Í", "I")


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
    assert "RODRIGUEZ" in data["reply"].replace("Í", "I")
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
    assert "RODRIGUEZ" in data["reply"].replace("Í", "I")
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


def test_robotina_manual_operaciones() -> None:
    for msg in ("Robotina dame tu manual de operaciones", "Robotina dame el manual"):
        res = client.post("/whatsapp/webhook", json={"message": msg})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["tipo_accion"] == "MANUAL_OPERACIONES"
        assert "MANUAL DE OPERACIONES" in data["reply"]
        assert "05:00 AM" in data["reply"]


def test_exportar_xlsx_y_comando_drive(tmp_path) -> None:
    from openpyxl import load_workbook
    from bitacora.excel_exporter import exportar_bitacora_xlsx

    out_xlsx = tmp_path / "test_bitacora.xlsx"
    exportar_bitacora_xlsx(output_path=out_xlsx)
    assert out_xlsx.exists()

    wb = load_workbook(out_xlsx)
    assert wb.sheetnames == ["Bitácora Guardia", "Pendientes Guardia", "Resumen"]
    assert wb["Bitácora Guardia"].max_column == 9
    assert wb["Bitácora Guardia"].max_row > 3
    assert wb["Bitácora Guardia"].cell(2, 1).value == "CENSO Y EQUIPO"

    res_dl = client.get("/descargar/bitacora.xlsx")
    assert res_dl.status_code == 200
    assert res_dl.content[:2] == b"PK"

    res_wa = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina graba la bitacora en xlsx en el drive"},
    )
    assert res_wa.status_code == 200
    data = res_wa.json()
    assert data["status"] == "success"
    assert data["tipo_accion"] == "EXPORTAR_XLSX_DRIVE"
    assert "bitacora.xlsx" in data["reply"]


def test_no_false_positive_patient_match_on_add() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={
            "message": "Robotina ayudame agregando a la bitácora al paciente Espinoza Manteca Jorge Hernan que se encuentra en el drive"
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert "ALMAGRO" not in data["reply"]
    assert "cama" in data["reply"].lower() or "no encontré" in data["reply"].lower()


def test_consulta_cama_6b_con_espacio() -> None:
    res = client.post(
        "/whatsapp/webhook",
        json={"message": "Robotina como esta 6B"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "CABEZAS" in data["reply"]


def test_drive_evolutions_parser_and_14h_scheduler(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timezone
    from bitacora import reminders
    from bitacora.drive_evolutions_sync import DEFAULT_EVOLUTIONS_FOLDER_IDS, _extract_latest_evolution

    assert "1hHA5TOBhrQOk08c3x9wCL7AuCgLEQ3qD" in DEFAULT_EVOLUTIONS_FOLDER_IDS  # Dr. Gaibor
    assert "1hNWIU4oCkJK4o6L0h-0yhKM4EgYT3D1l" in DEFAULT_EVOLUTIONS_FOLDER_IDS  # Dr. Torres
    assert "1iTf4TwBVllqAVHOrWi9Ba6qvS18Vr8u5" in DEFAULT_EVOLUTIONS_FOLDER_IDS  # Dr. Mosquera

    state_file = tmp_path / "auto_sync_14h_state.json"
    multi_state_file = tmp_path / "auto_sync_state.json"
    monkeypatch.setattr(reminders, "AUTO_SYNC_14H_STATE_FILE", state_file)
    monkeypatch.setattr(reminders, "AUTO_SYNC_STATE_FILE", multi_state_file)

    # 18:59 UTC == 13:59 Ecuador -> aún no corre
    dt_1359_ec = datetime(2026, 11, 17, 18, 59, tzinfo=timezone.utc).astimezone(reminders.ECUADOR_TZ)
    assert reminders.should_run_daily_14h_sync(ahora=dt_1359_ec) is False
    assert reminders.should_run_scheduled_sync(ahora=dt_1359_ec) is None

    # 19:00 UTC == 14:00 Ecuador -> sí corre '14:00'
    dt_1400_ec = datetime(2026, 11, 17, 19, 0, tzinfo=timezone.utc).astimezone(reminders.ECUADOR_TZ)
    assert reminders.should_run_daily_14h_sync(ahora=dt_1400_ec) is True
    reminders.mark_daily_14h_sync_done(ahora=dt_1400_ec)
    assert reminders.should_run_daily_14h_sync(ahora=dt_1400_ec) is False

    assert reminders.should_run_scheduled_sync(ahora=dt_1400_ec) == "14:00"
    reminders.mark_scheduled_sync_done("14:00", ahora=dt_1400_ec)
    assert reminders.should_run_scheduled_sync(ahora=dt_1400_ec) is None

    # 02:59 UTC del día siguiente == 21:59 Ecuador -> aún no corre 22:00
    dt_2159_ec = datetime(2026, 11, 18, 2, 59, tzinfo=timezone.utc).astimezone(reminders.ECUADOR_TZ)
    assert reminders.should_run_scheduled_sync(ahora=dt_2159_ec) is None

    # 03:00 UTC del día siguiente == 22:00 Ecuador -> sí corre '22:00'
    dt_2200_ec = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc).astimezone(reminders.ECUADOR_TZ)
    assert reminders.should_run_scheduled_sync(ahora=dt_2200_ec) == "22:00"
    reminders.mark_scheduled_sync_done("22:00", ahora=dt_2200_ec)
    assert reminders.should_run_scheduled_sync(ahora=dt_2200_ec) is None

    sample_doc = (
        "EVOLUCION PM DE MEDICINA INTERNA\n"
        "DG:\n- SEPSIS URINARIA\n"
        "O:\nTA: 120/70 MMHG\nFC: 88X\n"
        "A:\nPaciente estable en mejoría clínica.\n"
        "P:\n- PENDIENTE: TAC BODY S/C\n- CONTROL BH MAÑANA\n"
    )
    parsed = _extract_latest_evolution(sample_doc)
    assert "TA: 120/70 MMHG" in parsed["sv"]
    assert "estable" in parsed["analisis"]
    assert any("TAC BODY" in p for p in parsed["pendientes"])


def test_horario_guardias_responsables_en_excel(tmp_path) -> None:
    from datetime import date
    import openpyxl
    from bitacora.excel_exporter import exportar_bitacora_xlsx
    from bitacora.schedule_loader import obtener_responsables_guardia

    res = obtener_responsables_guardia(fecha=date(2026, 11, 17), refresh_drive=False)
    assert res["ok"] is True
    joined = " | ".join(res["medicos_guardia"])
    assert "SALAZAR ESCOBAR JESSICA FERNANDA" in joined
    assert "YUMBOPATIN RAMOS JONATHAN" in joined

    out_xlsx = tmp_path / "test_bitacora.xlsx"
    exportar_bitacora_xlsx(output_path=out_xlsx)
    wb = openpyxl.load_workbook(out_xlsx, data_only=True)
    ws = wb["Bitácora Guardia"]
    censo_equipo_cell = str(ws.cell(2, 2).value or "")
    assert "RESPONSABLES DE GUARDIA" in censo_equipo_cell
    assert "SALAZAR ESCOBAR JESSICA FERNANDA" in censo_equipo_cell


def test_nota_robotina_en_google_doc(monkeypatch) -> None:
    from bitacora import drive_evolutions_sync as des
    from bitacora.bed_store import get_cama

    pac = get_cama("6B")
    assert pac is not None

    nota = des.format_robotina_doc_note(pac, "RESULTADO", "Lactato 1.4 mmol/L")
    assert "NOTA DE ROBOTINA" in nota
    assert "Cama 6B" in nota
    assert "Lactato 1.4 mmol/L" in nota

    # Sin credenciales debe retornar needs_credentials=True sin lanzar excepción
    monkeypatch.setattr(des, "_load_drive_service_account_info", lambda: None)
    res_no_creds = des.escribir_nota_robotina_en_drive_doc(pac, "RESULTADO", "Lactato 1.4")
    assert res_no_creds["ok"] is False
    assert res_no_creds.get("needs_credentials") is True

    # Sincronización completa con doc mockeado no debe fallar por frozen_instance
    monkeypatch.setattr(
        des,
        "_fetch_docs_via_service_account",
        lambda fid: [
            {
                "id": "doc-6b",
                "name": f"6B {pac.nombre_completo}",
                "text": "EVOLUCION PM\nO:\nTA: 118/72\nA:\nEstable.\nP:\n- PENDIENTE: CONTROL BH\n",
            }
        ]
        if fid == des.DEFAULT_EVOLUTIONS_FOLDER_IDS[0]
        else [],
    )
    res_sync = des.sincronizar_evoluciones_drive(regenerar_reportes=False)
    assert res_sync["ok"] is True
    assert "6B" in res_sync["updated_beds"]












