"""Exportador de la bitácora médica consolidada a Excel (.xlsx) y carga a Google Drive."""

import json
import logging
import os
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from bitacora.bed_store import consolidar_bitacora
from bitacora.reminders import now_ecuador

logger = logging.getLogger(__name__)

DEFAULT_DRIVE_FOLDER_ID = os.getenv(
    "GOOGLE_DRIVE_FOLDER_ID",
    "109c_c3fDUqSXfE-4qM1zWB74SYNhXe9y",
)
SERVICE_ACCOUNT_FILE = Path("data/google_service_account.json")


def exportar_bitacora_xlsx(
    bitacora_path: Path | None = None,
    output_path: Path = Path("reports/bitacora_guardia.xlsx"),
) -> Path:
    """Consolida las camas activas y genera un archivo Excel (.xlsx) estructurado."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bitacora = consolidar_bitacora(sync_monolith_path=bitacora_path)
    ahora_ec = now_ecuador()

    wb = Workbook()

    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    wrap_top = Alignment(vertical="top", wrap_text=True)

    # --- HOJA 1: Censo Activo ---
    ws_censo = wb.active
    ws_censo.title = "Censo Activo"
    headers_censo = [
        "Cama",
        "Área",
        "Paciente",
        "Edad",
        "Diagnósticos Activos",
        "Medicación",
        "Últimos Laboratorios",
        "Evolución / Notas",
        "Alertas Críticas",
        "Pendientes",
    ]
    ws_censo.append(headers_censo)
    for col_idx in range(1, len(headers_censo) + 1):
        cell = ws_censo.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for p in bitacora.pacientes:
        meds_str = "; ".join(f"{m.nombre} {m.dosis}".strip() for m in p.medicaciones if not m.suspendido)
        labs_str = "\n".join(
            f"{l.tipo_estudio} ({l.fecha_hora}): "
            + ", ".join(f"{k}: {v}" for k, v in l.valores.items())
            for l in p.laboratorios[-4:]
        )
        obs_str = "\n".join(p.observaciones[-5:])
        alertas_str = "\n".join(p.alertas_criticas)
        pend_str = "\n".join(
            f"[{pend.estado}] ({pend.prioridad.value}) {pend.descripcion}"
            + (f" -> {pend.resultado_obtenido}" if pend.resultado_obtenido else "")
            for pend in p.pendientes
        )
        ws_censo.append(
            [
                p.cama,
                p.area,
                p.nombre_completo,
                p.edad,
                "; ".join(p.diagnosticos_activos),
                meds_str,
                labs_str,
                obs_str,
                alertas_str,
                pend_str,
            ]
        )

    widths_censo = [10, 22, 28, 8, 32, 28, 32, 36, 26, 36]
    for idx, width in enumerate(widths_censo, start=1):
        ws_censo.column_dimensions[get_column_letter(idx)].width = width
    for row in ws_censo.iter_rows(min_row=2, max_row=ws_censo.max_row):
        for cell in row:
            cell.alignment = wrap_top
    ws_censo.freeze_panes = "A2"
    ws_censo.auto_filter.ref = f"A1:{get_column_letter(len(headers_censo))}{max(1, ws_censo.max_row)}"

    # --- HOJA 2: Pendientes Guardia ---
    ws_pend = wb.create_sheet("Pendientes Guardia")
    headers_pend = ["Cama", "Paciente", "Área", "Prioridad", "Horario", "Estado", "Descripción", "Resultado"]
    ws_pend.append(headers_pend)
    for col_idx in range(1, len(headers_pend) + 1):
        cell = ws_pend.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    total_pendientes = 0
    total_completados = 0
    for p in bitacora.pacientes:
        for pend in p.pendientes:
            total_pendientes += 1
            if pend.estado == "COMPLETADO":
                total_completados += 1
            ws_pend.append(
                [
                    p.cama,
                    p.nombre_completo,
                    p.area,
                    pend.prioridad.value,
                    pend.horario_programado or "",
                    pend.estado,
                    pend.descripcion,
                    pend.resultado_obtenido or "",
                ]
            )

    widths_pend = [10, 28, 22, 12, 12, 14, 42, 32]
    for idx, width in enumerate(widths_pend, start=1):
        ws_pend.column_dimensions[get_column_letter(idx)].width = width
    for row in ws_pend.iter_rows(min_row=2, max_row=ws_pend.max_row):
        for cell in row:
            cell.alignment = wrap_top
    ws_pend.freeze_panes = "A2"
    ws_pend.auto_filter.ref = f"A1:{get_column_letter(len(headers_pend))}{max(1, ws_pend.max_row)}"

    # --- HOJA 3: Resumen ---
    ws_res = wb.create_sheet("Resumen")
    ws_res.append(["Campo", "Valor"])
    for col_idx in (1, 2):
        cell = ws_res.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font

    por_area: dict[str, int] = {}
    for p in bitacora.pacientes:
        por_area[p.area] = por_area.get(p.area, 0) + 1

    resumen_rows = [
        ("Periodo de Guardia", bitacora.periodo),
        ("Generado (Ecuador UTC-5)", ahora_ec.strftime("%Y-%m-%d %H:%M:%S")),
        ("Total Pacientes Activos", len(bitacora.pacientes)),
        ("Total Pendientes Registrados", total_pendientes),
        ("Pendientes Completados", total_completados),
    ]
    for area_name, count in sorted(por_area.items()):
        resumen_rows.append((f"Camas en {area_name}", count))

    for k, v in resumen_rows:
        ws_res.append([k, v])
    ws_res.column_dimensions["A"].width = 32
    ws_res.column_dimensions["B"].width = 40

    wb.save(str(output_path))
    logger.info(f"Excel XLSX generado en {output_path} ({len(bitacora.pacientes)} pacientes)")
    return output_path


def _load_drive_service_account_info() -> dict[str, Any] | None:
    """Carga el JSON de Service Account desde GOOGLE_SERVICE_ACCOUNT_JSON o archivo local."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        if raw.startswith("{"):
            return json.loads(raw)
        p = Path(raw)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    if SERVICE_ACCOUNT_FILE.exists():
        return json.loads(SERVICE_ACCOUNT_FILE.read_text(encoding="utf-8"))
    return None


def subir_xlsx_a_drive(
    xlsx_path: Path,
    folder_id: str | None = None,
    file_name: str | None = None,
) -> dict[str, Any]:
    """Sube o actualiza el archivo .xlsx en la carpeta oficial de Google Drive."""
    if not xlsx_path.exists():
        return {"ok": False, "error": f"No existe el archivo {xlsx_path}"}

    target_folder = folder_id or DEFAULT_DRIVE_FOLDER_ID
    target_name = file_name or f"bitacora_guardia_{now_ecuador().strftime('%Y-%m-%d')}.xlsx"
    sa_info = _load_drive_service_account_info()

    if not sa_info:
        return {
            "ok": False,
            "needs_credentials": True,
            "folder_id": target_folder,
            "error": (
                "Falta configurar GOOGLE_SERVICE_ACCOUNT_JSON en Railway para subir automáticamente a Drive."
            ),
        }

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        query = (
            f"name = '{target_name}' and '{target_folder}' in parents and trashed = false"
        )
        existing = (
            service.files()
            .list(
                q=query,
                fields="files(id, name, webViewLink)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get("files", [])
        )

        media = MediaFileUpload(
            str(xlsx_path),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            resumable=True,
        )

        if existing:
            file_id = existing[0]["id"]
            updated = (
                service.files()
                .update(
                    fileId=file_id,
                    media_body=media,
                    fields="id, name, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return {
                "ok": True,
                "action": "updated",
                "file_id": updated["id"],
                "name": updated["name"],
                "webViewLink": updated.get(
                    "webViewLink",
                    f"https://drive.google.com/file/d/{updated['id']}/view",
                ),
            }

        metadata = {"name": target_name, "parents": [target_folder]}
        created = (
            service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return {
            "ok": True,
            "action": "created",
            "file_id": created["id"],
            "name": created["name"],
            "webViewLink": created.get(
                "webViewLink",
                f"https://drive.google.com/file/d/{created['id']}/view",
            ),
        }
    except Exception as e:
        logger.error(f"Error subiendo XLSX a Google Drive: {e}")
        return {"ok": False, "error": str(e)}
