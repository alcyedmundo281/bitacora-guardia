"""Lector del horario mensual de guardias (Excel en Drive o local) para obtener responsables del día."""

import io
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from bitacora.reminders import now_ecuador

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE_FILE_ID = os.getenv(
    "GOOGLE_SCHEDULE_FILE_ID",
    "1vhRMbk9Dp_ODO3RW_Yl0z-NQnWZe5JZS",
)
LOCAL_SCHEDULE_PATH = Path("data/horario_guardias.xlsx")


def _refresh_schedule_from_drive(dest_path: Path = LOCAL_SCHEDULE_PATH) -> bool:
    """Intenta descargar el horario actualizado desde Google Drive usando Service Account."""
    try:
        from bitacora.excel_exporter import _load_drive_service_account_info

        sa_info = _load_drive_service_account_info()
        if not sa_info or not DEFAULT_SCHEDULE_FILE_ID:
            return False

        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload

        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        req = service.files().get_media(fileId=DEFAULT_SCHEDULE_FILE_ID, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        if buf.tell() > 0:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(buf.getvalue())
            logger.info(f"Horario de guardias actualizado desde Drive en {dest_path}")
            return True
    except Exception as e:
        logger.warning(f"No se pudo refrescar horario desde Drive (se usará copia local): {e}")
    return False


def obtener_responsables_guardia(
    fecha: date | datetime | None = None,
    schedule_path: Path = LOCAL_SCHEDULE_PATH,
    refresh_drive: bool = False,
) -> dict[str, Any]:
    """Retorna los responsables de guardia del día según el Excel de horario mensual."""
    if refresh_drive:
        _refresh_schedule_from_drive(schedule_path)

    if not schedule_path.exists():
        return {"ok": False, "medicos_guardia": [], "guardia_24h": [], "guardia_nocturna": [], "equipo_diurno": []}

    target_date = fecha.date() if isinstance(fecha, datetime) else (fecha or now_ecuador().date())
    dia = target_date.day

    try:
        wb = load_workbook(schedule_path, data_only=True)
        ws = wb[wb.sheetnames[0]]
    except Exception as e:
        logger.error(f"Error leyendo horario {schedule_path}: {e}")
        return {"ok": False, "medicos_guardia": [], "guardia_24h": [], "guardia_nocturna": [], "equipo_diurno": []}

    # Localizar fila de días (1..31)
    day_col: int | None = None
    header_row: int | None = None
    for r in range(1, min(ws.max_row + 1, 15)):
        cols_found = {}
        for c in range(2, ws.max_column + 1):
            val = ws.cell(r, c).value
            if val is not None and str(val).strip().isdigit():
                num = int(str(val).strip())
                if 1 <= num <= 31:
                    cols_found[num] = c
        if len(cols_found) >= 15 and dia in cols_found:
            header_row = r
            day_col = cols_found[dia]
            break

    if day_col is None or header_row is None:
        return {"ok": False, "medicos_guardia": [], "guardia_24h": [], "guardia_nocturna": [], "equipo_diurno": []}

    guardia_24h: list[str] = []
    guardia_nocturna: list[str] = []
    guardia_diurna_12h: list[str] = []
    equipo_diurno: list[str] = []

    role = "Posgradista"
    for r in range(header_row + 1, ws.max_row + 1):
        raw_name = ws.cell(r, 2).value
        if not raw_name:
            continue
        name = " ".join(str(raw_name).split()).strip()
        up_name = name.upper()

        if "ASISTENCIAL" in up_name:
            role = "Asistencial"
            continue
        if "POSGRADISTA" in up_name:
            role = "Posgradista"
            continue
        # Fin de tabla de médicos (leyenda de códigos)
        if ":" in name or "HORAS" in up_name or "LASLUISA" in up_name:
            break

        code_val = ws.cell(r, day_col).value
        if code_val is None:
            continue
        try:
            code = int(str(code_val).strip())
        except ValueError:
            continue

        label = f"{name} ({role})"
        if code == 30:
            guardia_24h.append(f"{label} — 24h [07:00-07:00]")
        elif code == 28:
            guardia_nocturna.append(f"{label} — Nocturna [19:00-07:30]")
        elif code == 26:
            guardia_diurna_12h.append(f"{label} — Diurna 12h [07:00-19:30]")
        elif code in (6, 9, 13, 16, 17, 23, 33):
            equipo_diurno.append(f"{label} — Turno {code}")

    medicos_guardia = guardia_24h + guardia_nocturna + guardia_diurna_12h
    if not medicos_guardia:
        medicos_guardia = equipo_diurno[:4]

    return {
        "ok": True,
        "fecha": target_date.strftime("%d/%m/%Y"),
        "medicos_guardia": medicos_guardia,
        "guardia_24h": guardia_24h,
        "guardia_nocturna": guardia_nocturna,
        "guardia_diurna_12h": guardia_diurna_12h,
        "equipo_diurno": equipo_diurno,
    }
