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

import unicodedata

DEFAULT_EVOLUTIONS_FOLDER_IDS = [
    "1hHA5TOBhrQOk08c3x9wCL7AuCgLEQ3qD",  # DR. GAIBOR ***NOVIEMBRE 2026***
    "1hNWIU4oCkJK4o6L0h-0yhKM4EgYT3D1l",  # DR TORRES 2026
    "1iTf4TwBVllqAVHOrWi9Ba6qvS18Vr8u5",  # DR. MOSQUERA 10/2026
]
EVOLUTIONS_FOLDER_ID = os.getenv(
    "GOOGLE_EVOLUTIONS_FOLDER_ID",
    DEFAULT_EVOLUTIONS_FOLDER_IDS[0],
)
EXCLUDED_FOLDERS = {"ALTAS", "PRESENTACIONES"}


def _get_target_folder_ids(folder_id: str | None = None) -> list[str]:
    if folder_id:
        return [f.strip() for f in folder_id.split(",") if f.strip()]
    env_multi = os.getenv("GOOGLE_EVOLUTIONS_FOLDER_IDS", "").strip()
    if env_multi:
        return [f.strip() for f in env_multi.split(",") if f.strip()]
    env_single = os.getenv("GOOGLE_EVOLUTIONS_FOLDER_ID", "").strip()
    if env_single:
        return [env_single]
    return list(DEFAULT_EVOLUTIONS_FOLDER_IDS)


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


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
    doc_norm = re.sub(r"[^A-Z0-9 ]", " ", _strip_accents(doc_name.upper())).split()
    doc_tokens = {t for t in doc_norm if len(t) > 3}
    best_pac = None
    best_score = 0
    for p in pacientes:
        p_words = re.findall(r"[A-Z0-9]+", _strip_accents(p.nombre_completo.upper()))
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
    """Lee los Google Docs de evoluciones en Drive (Gaibor, Torres, Mosquera), actualiza alta frecuencia y regenera PDF/XLSX."""
    target_folders = _get_target_folder_ids(folder_id)
    docs_by_id: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    source = "service_account"

    for fid in target_folders:
        fetched: list[dict[str, str]] = []
        try:
            fetched = _fetch_docs_via_service_account(fid)
        except Exception as e:
            logger.error(f"Error leyendo carpeta Drive {fid} con Service Account: {e}")
            errors.append(f"{fid}: {e}")
        if not fetched:
            try:
                fetched = _fetch_docs_via_gdrive_cli(fid)
                if fetched:
                    source = "gdrive_cli"
            except Exception as e:
                errors.append(f"cli {fid}: {e}")
        for d in fetched:
            docs_by_id[d["id"]] = d

    docs = list(docs_by_id.values())
    if not docs:
        return {
            "ok": False,
            "needs_credentials": not bool(_load_drive_service_account_info()),
            "folder_ids": target_folders,
            "updated_beds": [],
            "errors": errors,
            "reason": "No se encontraron documentos accesibles en las carpetas de evoluciones (o falta GOOGLE_SERVICE_ACCOUNT_JSON en Railway).",
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
        "folder_ids": target_folders,
        "docs_leidos": len(docs),
        "updated_beds": updated_beds,
        "drive_upload": drive_res,
    }


_DOC_INDEX_CACHE: dict[str, Any] = {"ts": 0.0, "docs": []}
_DOC_INDEX_TTL_SECONDS = 600.0


def _list_drive_doc_headers(sa_info: dict[str, Any], force_refresh: bool = False) -> list[dict[str, str]]:
    """Lista (id, name) de los Google Docs en las carpetas de Gaibor, Torres y Mosquera con caché de 10 min."""
    import time

    now_ts = time.monotonic()
    if not force_refresh and _DOC_INDEX_CACHE["docs"] and (now_ts - _DOC_INDEX_CACHE["ts"]) < _DOC_INDEX_TTL_SECONDS:
        return list(_DOC_INDEX_CACHE["docs"])

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    docs_by_id: dict[str, dict[str, str]] = {}
    for fid in _get_target_folder_ids():
        q = (
            f"'{fid}' in parents and trashed = false "
            "and mimeType = 'application/vnd.google-apps.document'"
        )
        files = (
            service.files()
            .list(
                q=q,
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get("files", [])
        )
        for f in files:
            docs_by_id[f["id"]] = {"id": f["id"], "name": f["name"]}

    result = list(docs_by_id.values())
    _DOC_INDEX_CACHE["ts"] = now_ts
    _DOC_INDEX_CACHE["docs"] = result
    return result


def format_robotina_doc_note(paciente: Paciente, categoria: str, detalle: str) -> str:
    """Formatea el bloque de Nota de Robotina con fecha/hora de Ecuador."""
    ahora_str = now_ecuador().strftime("%d/%m/%Y %H:%M")
    return (
        f"\n\n--- NOTA DE ROBOTINA ({ahora_str} Ecuador | {categoria}) ---\n"
        f"• Cama {paciente.cama}: {detalle.strip()}\n"
    )


def escribir_nota_robotina_en_drive_doc(
    paciente: Paciente,
    categoria: str,
    detalle: str,
) -> dict[str, Any]:
    """Agrega una 'Nota de Robotina' al final del Google Doc del paciente en Drive."""
    sa_info = _load_drive_service_account_info()
    if not sa_info:
        return {"ok": False, "needs_credentials": True, "reason": "Falta GOOGLE_SERVICE_ACCOUNT_JSON"}

    try:
        headers = _list_drive_doc_headers(sa_info)
        matched_doc: dict[str, str] | None = None
        for h in headers:
            if _match_patient_for_doc(h["name"], [paciente]):
                matched_doc = h
                break

        if not matched_doc:
            # Reintentar refrescando caché por si se creó un doc nuevo hoy
            headers = _list_drive_doc_headers(sa_info, force_refresh=True)
            for h in headers:
                if _match_patient_for_doc(h["name"], [paciente]):
                    matched_doc = h
                    break

        if not matched_doc:
            return {"ok": False, "reason": f"No se encontró Google Doc en Drive para {paciente.cama} ({paciente.nombre_completo})"}

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/documents"],
        )
        docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
        nota_texto = format_robotina_doc_note(paciente, categoria, detalle)

        docs_service.documents().batchUpdate(
            documentId=matched_doc["id"],
            body={
                "requests": [
                    {
                        "insertText": {
                            "endOfSegmentLocation": {"segmentId": ""},
                            "text": nota_texto,
                        }
                    }
                ]
            },
        ).execute()

        logger.info(f"Nota de Robotina escrita en Google Doc '{matched_doc['name']}' ({matched_doc['id']})")
        return {"ok": True, "doc_id": matched_doc["id"], "doc_name": matched_doc["name"]}
    except Exception as e:
        logger.warning(f"No se pudo escribir Nota de Robotina en Google Doc para {paciente.cama}: {e}")
        return {"ok": False, "error": str(e)}

