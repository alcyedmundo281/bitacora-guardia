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
from bitacora.schedule_loader import obtener_responsables_guardia

logger = logging.getLogger(__name__)

DEFAULT_DRIVE_FOLDER_ID = os.getenv(
    "GOOGLE_DRIVE_FOLDER_ID",
    "109c_c3fDUqSXfE-4qM1zWB74SYNhXe9y",
)
SERVICE_ACCOUNT_FILE = Path("data/google_service_account.json")


_IMAGEN_KEYWORDS = ("TAC", "TC ", "TOMOGRAF", "UROTAC", "ECO", "ECOGRAF", "RX", "PLACA", "RMN", "RESONANCIA", "PET", "DOPPLER", "EKG", "ELECTROCARDIOGRAMA", "ENDOSCOPIA", "COLONOSCOPIA", "EDA")


def _is_imagen(texto: str) -> bool:
    up = texto.upper()
    return any(k in up for k in _IMAGEN_KEYWORDS)


def exportar_bitacora_xlsx(
    bitacora_path: Path | None = None,
    output_path: Path = Path("reports/bitacora_guardia.xlsx"),
) -> Path:
    """Consolida las camas activas y genera un archivo Excel (.xlsx) con el formato clínico de 9 columnas."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bitacora = consolidar_bitacora(sync_monolith_path=bitacora_path)
    ahora_ec = now_ecuador()
    guardia_hoy = obtener_responsables_guardia(ahora_ec, refresh_drive=True)
    if guardia_hoy.get("medicos_guardia"):
        bitacora = bitacora.model_copy(update={"medicos_guardia": guardia_hoy["medicos_guardia"]})

    wb = Workbook()

    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=10)
    summary_fill = PatternFill(start_color="EFF6FF", end_color="EFF6FF", fill_type="solid")
    section_fill = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
    section_font = Font(color="1E3A8A", bold=True, size=10)
    wrap_top = Alignment(vertical="top", wrap_text=True)

    # --- HOJA 1: Bitácora Guardia (9 columnas: Col 1-4 Baja Frecuencia | Col 5-9 Alta Frecuencia) ---
    ws = wb.active
    ws.title = "Bitácora Guardia"
    headers = [
        "1. HABITACIÓN",
        "2. DATOS DEL PACIENTE, DÍAS DE HOSPITALIZACIÓN",
        "3. ANTECEDENTES PATOLÓGICOS PERSONALES",
        "4. DIAGNÓSTICOS ACTIVOS (ORDEN JERÁRQUICO)",
        "5. MEDICACIÓN (INCLUYE TFGe CKD-EPI)",
        "6. PENDIENTES A REALIZAR",
        "7. EXÁMENES DE LABORATORIO (CRONOLÓGICO Y RESUMIDO)",
        "8. EXÁMENES DE IMAGEN (CRONOLÓGICO Y RESUMIDO)",
        "9. PENDIENTES GENERALES A REVISAR Y TOMAR EN CUENTA GUARDIA PM",
    ]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    hosp_list = [p for p in bitacora.pacientes if p.area == "HOSPITALIZACION"]
    perif_list = [p for p in bitacora.pacientes if p.area == "PERIFERICOS"]
    emerg_list = [p for p in bitacora.pacientes if p.area == "EMERGENCIA_OBSERVACION"]

    alertas_censo = []
    for p in bitacora.pacientes:
        for al in p.alertas_criticas[:1]:
            alertas_censo.append(f"• {p.cama} ({p.nombre_completo.split()[0]}): {al}")

    # Fila 2: CENSO Y EQUIPO
    medicos_txt = "\n".join(f"• {m}" for m in bitacora.medicos_guardia) if bitacora.medicos_guardia else "• Equipo de Guardia"
    censo_row = [
        "CENSO Y EQUIPO",
        f"BITÁCORA MEDICINA INTERNA {bitacora.periodo}\nRESPONSABLES DE GUARDIA ({ahora_ec.strftime('%d/%m/%Y')}):\n{medicos_txt}",
        (
            "CENSO:\n"
            f"• Emergencia: {len(emerg_list)}\n"
            f"• Hospitalizados: {len(hosp_list)}\n"
            f"• Periféricos: {len(perif_list)}\n"
            f"• Total activos: {len(bitacora.pacientes)}"
        ),
        "SISTEMA DE CULTIVOS:\nhttp://172.16.98.198/ACCOUNT/SIGNIN",
        f"ACTUALIZADO (Ecuador):\n{ahora_ec.strftime('%d/%m/%Y %H:%M')}",
        "PRIORIDADES GUARDIA PM:\n• Recabar laboratorios de control\n• Revisar informes de imagen\n• Seguimiento de interconsultas",
        "\n".join(alertas_censo[:6]) if alertas_censo else "Sin alertas críticas registradas",
        "ESTUDIOS Y PROCEDIMIENTOS:\nVer Columna 8 por paciente",
        "REVISIÓN DE GUARDIA:\nVigilar alertas clínicas, balances hídricos y techos terapéuticos.",
    ]
    ws.append(censo_row)
    for col_idx in range(1, 10):
        cell = ws.cell(row=2, column=col_idx)
        cell.fill = summary_fill
        cell.alignment = wrap_top
        if col_idx == 1:
            cell.font = Font(bold=True, size=10)

    secciones = [
        ("SECCIÓN I: HOSPITALIZACIÓN — PISO MEDICINA INTERNA", hosp_list),
        ("SECCIÓN II: PERIFÉRICOS", perif_list),
        ("SECCIÓN III: EMERGENCIA / OBSERVACIÓN", emerg_list),
    ]

    total_pendientes = 0
    total_completados = 0

    for sec_title, sec_pacientes in secciones:
        if not sec_pacientes:
            continue
        sec_row_idx = ws.max_row + 1
        ws.append([
            f"{sec_title} (TOTAL {len(sec_pacientes)} REGISTROS)",
            "DATOS DEL PACIENTE Y DÍAS DE HOSPITALIZACIÓN",
            "ANTECEDENTES PATOLÓGICOS PERSONALES",
            "DIAGNÓSTICOS ACTIVOS (ORDEN JERÁRQUICO)",
            "MEDICACIÓN Y TFGe CKD-EPI",
            "PENDIENTES A REALIZAR",
            "LABORATORIOS (CRONOLÓGICO Y RESUMIDO)",
            "IMAGEN (CRONOLÓGICO Y RESUMIDO)",
            "PENDIENTES GENERALES GUARDIA PM",
        ])
        for col_idx in range(1, 10):
            c = ws.cell(row=sec_row_idx, column=col_idx)
            c.fill = section_fill
            c.font = section_font
            c.alignment = wrap_top

        for p in sec_pacientes:
            # Col 2: Datos del paciente (Baja frecuencia)
            datos_lines = [p.nombre_completo]
            if p.hc and p.hc != "N/D":
                datos_lines.append(f"HC: {p.hc}")
            if p.cedula:
                datos_lines.append(f"CI: {p.cedula}")
            if p.edad and p.edad != "N/D":
                datos_lines.append(f"Edad: {p.edad}")
            if p.dias_hospitalizacion is not None:
                datos_lines.append(f"Días de hospitalización: {p.dias_hospitalizacion} días")

            # Col 3: Antecedentes (Baja frecuencia)
            app_items = [x for x in p.app if not x.upper().startswith("ANTECEDENTES")]
            if p.alergias:
                app_items.append(f"Alergias: {', '.join(p.alergias)}")
            app_txt = "\n".join(app_items) if app_items else "Sin antecedentes registrados"

            # Col 4: Diagnósticos activos numerados (Baja frecuencia)
            dx_items = [x for x in p.diagnosticos_activos if not x.upper().startswith("DIAGN")]
            dx_txt = "\n".join(f"{i}. {d}" for i, d in enumerate(dx_items, start=1)) if dx_items else "Por definir"

            # Col 5: Medicación vigente (Alta frecuencia)
            meds_txt = "\n".join(
                f"• {m.nombre} {m.dosis}".strip() for m in p.medicaciones if not m.suspendido
            ) or "Sin medicación registrada"

            # Col 6 & Col 8: Pendientes a realizar vs Estudios de Imagen (Alta frecuencia)
            pend_realizar: list[str] = []
            imagen_items: list[str] = []
            for pend in p.pendientes:
                total_pendientes += 1
                if pend.estado == "COMPLETADO":
                    total_completados += 1
                prefix = "✅ [COMPLETADO] " if pend.estado == "COMPLETADO" else "• "
                res_s = f" ➔ {pend.resultado_obtenido}" if pend.resultado_obtenido else ""
                line = f"{prefix}{pend.descripcion}{res_s}"
                if pend.tipo.value == "IMAGEN_PROCEDIMIENTO" or _is_imagen(pend.descripcion):
                    imagen_items.append(line)
                else:
                    pend_realizar.append(line)

            # Col 7: Laboratorios cronológicos (Alta frecuencia)
            labs_items = []
            for lab in p.laboratorios:
                vals = ", ".join(f"{k}: {v}" for k, v in lab.valores.items())
                labs_items.append(f"• {lab.fecha_hora} ({lab.tipo_estudio}): {vals}")

            # Col 9: Pendientes generales / Alertas / SV / Evolución PM (Alta frecuencia)
            pm_items = []
            for al in p.alertas_criticas:
                pm_items.append(f"⚠ {al}")
            for obs in p.observaciones[-5:]:
                pm_items.append(f"• {obs}")

            row_idx = ws.max_row + 1
            ws.append([
                p.cama,
                "\n".join(datos_lines),
                app_txt,
                dx_txt,
                meds_txt,
                "\n".join(pend_realizar) if pend_realizar else "Sin pendientes activos",
                "\n".join(labs_items) if labs_items else "Sin laboratorios recientes",
                "\n".join(imagen_items) if imagen_items else "Sin estudios de imagen pendientes",
                "\n".join(pm_items) if pm_items else "Estable, continuar vigilancia habitual.",
            ])
            for col_idx in range(1, 10):
                c = ws.cell(row=row_idx, column=col_idx)
                c.alignment = wrap_top
                if col_idx == 1:
                    c.font = Font(bold=True, size=10)

    widths = [14, 30, 30, 34, 36, 34, 38, 34, 40]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A2"

    # --- HOJA 2: Pendientes Guardia (vista filtrable por prioridad/estado) ---
    ws_pend = wb.create_sheet("Pendientes Guardia")
    headers_pend = ["Cama", "Paciente", "Área", "Prioridad", "Horario", "Estado", "Descripción", "Resultado"]
    ws_pend.append(headers_pend)
    for col_idx in range(1, len(headers_pend) + 1):
        cell = ws_pend.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for p in bitacora.pacientes:
        for pend in p.pendientes:
            ws_pend.append([
                p.cama,
                p.nombre_completo,
                p.area,
                pend.prioridad.value,
                pend.horario_programado or "",
                pend.estado,
                pend.descripcion,
                pend.resultado_obtenido or "",
            ])

    widths_pend = [12, 28, 22, 12, 12, 14, 42, 32]
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

    resumen_rows = [
        ("Periodo de Guardia", bitacora.periodo),
        ("Generado (Ecuador UTC-5)", ahora_ec.strftime("%Y-%m-%d %H:%M:%S")),
        ("Total Pacientes Activos", len(bitacora.pacientes)),
        ("Hospitalización", len(hosp_list)),
        ("Periféricos", len(perif_list)),
        ("Emergencia / Observación", len(emerg_list)),
        ("Total Pendientes Registrados", total_pendientes),
        ("Pendientes Completados", total_completados),
    ]
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
