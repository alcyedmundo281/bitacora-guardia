"""Sincronizador automático de evoluciones clínicas desde Google Docs en Drive hacia data/camas/*.json."""

import io
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bitacora.bed_store import consolidar_bitacora, get_cama, list_camas_activas, save_cama
from bitacora.excel_exporter import _load_drive_service_account_info, exportar_bitacora_xlsx, subir_xlsx_a_drive
from bitacora.models import Paciente, Pendiente, PendientePrioridad, PendienteTipo, TurnoObjetivo
from bitacora.pdf_generator import generar_bitacora_completa
from bitacora.reminders import now_ecuador

logger = logging.getLogger(__name__)

EVOLUTIONS_FOLDER_ID = os.getenv(
    "GOOGLE_EVOLUTIONS_FOLDER_ID",
    "1hHA5TOBhrQOk08c3x9wCL7AuCgLEQ3qD",
)
EXCLUDED_FOLDERS = {"ALTAS", "PRESENTACIONES"}


def _clean_block_lines(block: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", ln).strip(" •-*\t\\")
        for ln in block.splitlines()
        if ln.strip(" •-*\t.\\")
    ]


def _extract_latest_evolution(text: str) -> dict[str, Any]:
    """Extrae la última nota de evolución (SOAP + indicaciones) de un documento de paciente."""
    # Buscar el último bloque que comience con EVOLUCION o NOTA DE EVOLUCION
    matches = list(re.finditer(r"(?im)^(?:NOTA DE\s+)?EVOLUCI[ÓO]N\b.*$", text))
    section = text[matches[-1].start():] if matches else text[-4000:]

    def _grab(pattern: str) -> str:
        m = re.search(pattern, section, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ""

    dg_raw = _grab(r"\bDG\s*:\s*(.+?)(?=\n\s*\*?\*?S\s*:|\n\s*SUBJETIVO\s*:|\Z)")
    o_raw = _grab(r"\n\s*\*?\*?O\s*:\s*\*?\*?\s*(.+?)(?=\n\s*\*?\*?A\s*:|\n\s*ANALISIS|\Z)")
    a_raw = _grab(r"\n\s*\*?\*?A\s*:\s*\*?\*?\s*(.+?)(?=\n\s*\*?\*?P\s*:|\n\s*PLAN\s*:|\Z)")
    p_raw = _grab(r"\n\s*\*?\*?P\s*:\s*\*?\*?\s*(.+?)(?=\n\s*(?:MD|DR\.?|DRA\.?)\s+[A-ZÁÉÍÓÚÑ]{3,}|\Z)")

    # Buscar signos vitales en O:
    sv_lines = []
    for ln in _clean_block_lines(o_raw):
        if any(k in ln.upper() for k in ("TA:", "FC:", "FR:", "T:", "TEMP", "STO2", "SATO2", "GLASGOW")):
            sv_lines.append(ln.replace("**", "").strip())

    # Pendientes de P:
    pends: list[str] = []
    for ln in _clean_block_lines(p_raw):
        up = ln.upper()
        if any(k in up for k in ("PENDIENTE", "PD ", "TAC", "RMN", "ECO", "UROTAC", "URO-TAC", "MAÑANA", "BH", "CONTROL", "INTERCONSULTA", "IC ", "PASE A QUIR")):
            pends.append(ln)

    a_clean = re.sub(r"\s+", " ", a_raw).strip()
    return {
        "dx": _clean_block_lines(dg_raw)[:8],
        "sv": " | ".join(sv_lines[:6]),
        "analisis": a_clean[:320] if a_clean else "",
        "pendientes": pends[:8],
    }


def _match_patient_for_doc(doc_name: str, pacientes: list[Paciente]) -> Paciente | None:
    """Empareja un documento de Drive (ej: '10A MOSQUERA SALVADOR...') con una cama activa."""
    # 1. Intentar por código de cama al inicio del nombre del documento
    first_tok = doc_name.strip().split()[0].upper() if doc_name.strip() else ""
    if first_tok and re.match(r"^(?:\d{1,2}[A-D]?|ABV\d*|ABR\d*|AB\d+)$", first_tok):
        pac = get_cama(first_tok)
        if pac:
            return pac

    # 2. Emparejar por apellidos en el nombre del documento
    doc_norm = re.sub(r"[^A-Z0-9 ]", " ", doc_name.upper()).split()
    doc_tokens = {t for t in doc_norm if len(t) > 3}
    best_pac = None
    best_score = 0
    for p in pacientes:
        p_words = re.findall(r"[A-Z0-9]+", p.nombre_completo.upper())
        surnames = set(p_words[:2]) if len(p_words) >= 3 else set(p_words[:1])
        score = sum(2 if w in surnames else 1 for w in set(p_words) if w in doc_tokens)
        if score >= 2 and score > best_score:
            best_score = score
            best_pac = p
    return best_pac


def _fetch_docs_via_service_account(folder_id: str) -> list[dict[str, str]]:
    sa_info = _load_drive_service_account_info()
    if not sa_info:
        return []
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    q = (
        f"'{folder_id}' in parents and trashed = false "
        "and mimeType = 'application/vnd.google-apps.document'"
    )
    files = (
        service.files()
        .list(q=q, fields="files(id, name, modifiedTime)", supportsAllDrives=True, includeItemsFromAllDrives=True)
        .execute()
        .get("files", [])
    )

    out: list[dict[str, str]] = []
    for f in files:
        req = service.files().export_media(fileId=f["id"], mimeType="text/plain")
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        out.append({"id": f["id"], "name": f["name"], "text": buf.getvalue().decode("utf-8", errors="ignore")})
    return out


def _fetch_docs_via_gdrive_cli(folder_id: str) -> list[dict[str, str]]:
    if not shutil.which("gdrive"):
        return []
    res = subprocess.run(
        ["gdrive", "readonly", "ls", folder_id, "--json"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if res.returncode != 0:
        return []
    items = json.loads(res.stdout or "[]")
    if isinstance(items, dict):
        items = items.get("files", [])
    out: list[dict[str, str]] = []
    tmp_dir = Path("/tmp/drive_evoluciones")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        mime = str(item.get("mimeType") or item.get("type") or "")
        name = str(item.get("name") or "")
        fid = str(item.get("id") or "")
        if not fid or name.upper() in EXCLUDED_FOLDERS:
            continue
        if "document" in mime or "google-apps.document" in mime:
            tmp_file = tmp_dir / f"{fid}.txt"
            if tmp_file.exists():
                tmp_file.unlink()
            exp = subprocess.run(
                ["gdrive", "readonly", "export", fid, str(tmp_file), "--format", "txt"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if exp.returncode == 0 and tmp_file.exists():
                out.append({"id": fid, "name": name, "text": tmp_file.read_text(encoding="utf-8", errors="ignore")})
    return out


def sincronizar_evoluciones_drive(
    folder_id: str | None = None,
    regenerar_reportes: bool = True,
) -> dict[str, Any]:
    """Lee los Google Docs de evoluciones en Drive, actualiza alta frecuencia de camas y regenera PDF/XLSX."""
    target_folder = folder_id or EVOLUTIONS_FOLDER_ID
    docs = _fetch_docs_via_service_account(target_folder)
    source = "service_account"
    if not docs:
        docs = _fetch_docs_via_gdrive_cli(target_folder)
        source = "gdrive_cli"

    if not docs:
        return {
            "ok": False,
            "needs_credentials": not bool(_load_drive_service_account_info()),
            "updated_beds": [],
            "reason": "No se encontraron documentos accesibles en la carpeta de evoluciones (o falta GOOGLE_SERVICE_ACCOUNT_JSON en Railway).",
        }

    pacientes = list_camas_activas()
    updated_beds: list[str] = []
    ahora_str = now_ecuador().strftime("%d/%m/%Y %H:%M")

    for doc_item in docs:
        pac = _match_patient_for_doc(doc_item["name"], pacientes)
        if not pac:
            continue
        parsed = _extract_latest_evolution(doc_item["text"])

        # Alta frecuencia: actualizar evolución reciente y pendientes detectados sin duplicar
        new_obs: list[str] = []
        if parsed["sv"]:
            new_obs.append(f"SV ({ahora_str}): {parsed['sv']}")
        if parsed["analisis"]:
            new_obs.append(f"Evolución Drive ({ahora_str}): {parsed['analisis']}")
        if new_obs:
            pac.observaciones = (pac.observaciones[-3:] + new_obs)[-6:]

        existing_desc = {p.descripcion.upper() for p in pac.pendientes}
        for idx_offset, p_txt in enumerate(parsed["pendientes"], start=1):
            if p_txt.upper() not in existing_desc:
                prio = PendientePrioridad.STAT if "STAT" in p_txt.upper() else PendientePrioridad.URGENTE
                pac.pendientes.append(
                    Pendiente(
                        id=f"PEND-{re.sub(r'[^A-Z0-9]', '', pac.cama)}-EV{len(pac.pendientes) + idx_offset:02d}",
                        tipo=PendienteTipo.LABORATORIO,
                        descripcion=p_txt[:220],
                        prioridad=prio,
                        turno=TurnoObjetivo.NOCTURNA_20_24,
                        es_visita_nocturna=True,
                        estado="PENDIENTE",
                    )
                )

        save_cama(pac)
        updated_beds.append(pac.cama)

    drive_res: dict[str, Any] = {}
    if regenerar_reportes:
        consolidar_bitacora()
        generar_bitacora_completa()
        xlsx_path = exportar_bitacora_xlsx()
        drive_res = subir_xlsx_a_drive(xlsx_path)

    return {
        "ok": True,
        "source": source,
        "docs_leidos": len(docs),
        "updated_beds": updated_beds,
        "drive_upload": drive_res,
    }
