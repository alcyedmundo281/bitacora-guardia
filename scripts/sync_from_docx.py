"""Sincroniza las matrices individuales de camas (data/camas/*.json) desde el .docx de guardia."""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

import docx

from bitacora.bed_store import CAMAS_DIR, HISTORIAL_DIR, META_FILE, consolidar_bitacora, save_cama
from bitacora.models import (
    LaboratorioReciente,
    Medicacion,
    Paciente,
    Pendiente,
    PendientePrioridad,
    PendienteTipo,
    TurnoObjetivo,
)
from bitacora.pdf_generator import generar_bitacora_completa


def _clean_lines(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip(" •-.\t")
        for line in text.splitlines()
        if line.strip(" •-.\t\r\n")
    ]


def _extract_patient_info(col1: str) -> tuple[str, str, str, str | None]:
    raw = re.sub(r"\s+", " ", col1).strip()
    # HC
    m_hc = re.search(r"H\.?C\.?\s*:?\s*(\d+)", raw, re.IGNORECASE)
    hc = m_hc.group(1) if m_hc else "N/D"
    # CI
    m_ci = re.search(r"(?:C\.?I\.?|CEDULA|C[ÉE]DULA DE IDENTIDAD|C)\s*:?\s*(\d{8,10})", raw, re.IGNORECASE)
    ci = m_ci.group(1) if m_ci else None
    # Edad
    m_edad = re.search(r"EDAD\s*:?\s*(\d+\s*A[ÑN]O[S]?)|\b(\d{1,3})\s*A[ÑN]O[S]?\b", raw, re.IGNORECASE)
    edad = (m_edad.group(1) or m_edad.group(2)) if m_edad else "N/D"
    if edad != "N/D" and "AÑO" not in edad.upper():
        edad = f"{edad} años"

    # Nombre: tomar la parte antes de HC, CI, EDAD, F. NAC, etc.
    nombre_part = re.sub(r"^(?:NOMBRES?|NOMBRE)\s*:\s*", "", raw, flags=re.IGNORECASE)
    nombre_part = re.split(
        r"\b(?:H\.?C\.?|C\.?I\.?|CEDULA|C[ÉE]DULA|HISTORIA|EDAD|F\.?\s*NAC|GENERO|GÉNERO|F\s+MUJER|M\s+HOMBRE)\b|\b\d{8,10}\b",
        nombre_part,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    nombre = re.sub(r"\s+", " ", nombre_part).strip(" :-|")
    if not nombre:
        nombre = raw[:50]
    return nombre.upper(), hc, edad, ci


def _parse_pendientes(cama: str, col_pend: str, col_com: str) -> list[Pendiente]:
    pends: list[Pendiente] = []
    combined = _clean_lines(col_pend) + _clean_lines(col_com)
    idx = 1
    for item in combined:
        up = item.upper()
        if len(item) < 4 or up in ("M:", "E:", "C:", "L:", "PENDIENTE", "PENDIENTES"):
            continue
        prio = PendientePrioridad.RUTINA
        if any(w in up for w in ("STAT", "YA", "INMEDIATO", "CRITIC")):
            prio = PendientePrioridad.STAT
        elif any(w in up for w in ("URGENTE", "PENDIENTE", "HOY", "TAC", "ECO", "IC ", "INTERCONSULTA", "VALORACION", "VALORACIÓN")):
            prio = PendientePrioridad.URGENTE
        elif any(w in up for w in ("CONTROL", "05:00", "21H", "MAÑANA")):
            prio = PendientePrioridad.CONTROL

        tipo = PendienteTipo.LABORATORIO
        if any(w in up for w in ("TAC", "TC ", "RX", "ECO", "EKG", "PLACA", "HISTOPATOL")):
            tipo = PendienteTipo.IMAGEN_PROCEDIMIENTO
        elif any(w in up for w in ("IC ", "INTERCONSULTA", "VALORACION", "VALORACIÓN", "NEUMO", "REUMA", "CARDIO", "DERMATO", "ONCO")):
            tipo = PendienteTipo.INTERCONSULTA

        pends.append(
            Pendiente(
                id=f"PEND-{re.sub(r'[^A-Z0-9]', '', cama)}-{idx:02d}",
                tipo=tipo,
                descripcion=item[:220],
                prioridad=prio,
                turno=TurnoObjetivo.NOCTURNA_20_24,
                es_visita_nocturna=(prio in (PendientePrioridad.STAT, PendientePrioridad.URGENTE, PendientePrioridad.CONTROL)),
                estado="PENDIENTE",
            )
        )
        idx += 1
    return pends[:12]


def _parse_meds(col_med: str) -> list[Medicacion]:
    meds: list[Medicacion] = []
    for line in _clean_lines(col_med):
        up = line.upper()
        if len(line) < 4 or up.startswith(("D:", "I:", "CKD", "FILTRADO", "M:", "HIDRATAC")):
            continue
        if any(w in up for w in ("MG", "GR", "ML", "UI", "PUFF", "CADA", "QD", "BID", "TID", "IV", "VO", "SC", "PARACETAMOL", "OMEPRAZOL", "ENOXAPARINA", "MEROPENEM", "PIPER", "VANCOMICINA", "CEFTRIAXONA", "AMPICILINA")):
            meds.append(Medicacion(nombre=line[:120], dosis="", via="", frecuencia=""))
    return meds[:10]


def sync_docx_to_bed_store(docx_path: Path) -> int:
    doc = docx.Document(str(docx_path))

    # Extraer periodo y médicos de guardia
    periodo = datetime.now().strftime("%d/%m/%Y")
    medicos = ["Equipo de Guardia"]
    for p in doc.paragraphs[:15]:
        t = p.text.strip()
        if "GUARDIA DEL" in t.upper():
            periodo = re.sub(r"^GUARDIA\s+DEL\s+", "", t, flags=re.IGNORECASE).strip()
        elif "MD." in t.upper() or "PG " in t.upper():
            medicos = [m.strip() for m in re.split(r"[/,]", t) if m.strip()]

    # Archivar camas anteriores antes de reemplazar con el censo vivo
    if CAMAS_DIR.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = HISTORIAL_DIR / f"sync_{ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for old_f in CAMAS_DIR.glob("*.json"):
            shutil.move(str(old_f), str(backup_dir / old_f.name))
    else:
        CAMAS_DIR.mkdir(parents=True, exist_ok=True)

    META_FILE.write_text(
        json.dumps(
            {
                "version_schema": "1.0.0",
                "fecha_guardia": datetime.now().strftime("%Y-%m-%d"),
                "periodo": periodo,
                "medicos_guardia": medicos,
                "enlaces_utiles": {
                    "censo_drive": "https://docs.google.com/document/d/1dm-8m2VOlTZfwYeORxcFuTDuFi2xitho/edit"
                },
                "ingresos_guardia": 0,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    areas: list[Literal["HOSPITALIZACION", "PERIFERICOS", "EMERGENCIA_OBSERVACION"]] = [
        "HOSPITALIZACION",
        "PERIFERICOS",
        "EMERGENCIA_OBSERVACION",
    ]

    total = 0
    seen_beds: dict[str, int] = {}

    for t_idx, table in enumerate(doc.tables[:3]):
        area = areas[t_idx]
        for r_idx, row in enumerate(table.rows):
            if r_idx == 0:
                continue
            cells = [c.text.strip() for c in row.cells]
            if len(cells) < 2:
                continue
            col0 = re.sub(r"\s+", " ", cells[0]).strip()
            col1 = cells[1].strip()

            # Ignorar filas vacías o sin paciente real (ej: "ENCARGADO DE INFECTO")
            if not col1 or len(col1) < 6 or "ENCARGADO" in col0.upper() or "ENCARGADO" in col1.upper():
                continue

            cama_raw = col0.upper() if col0 else f"{area[:4]}_{r_idx}"
            if cama_raw in seen_beds:
                seen_beds[cama_raw] += 1
                cama_raw = f"{cama_raw}_{seen_beds[cama_raw]}"
            else:
                seen_beds[cama_raw] = 1

            nombre, hc, edad, ci = _extract_patient_info(col1)
            app = _clean_lines(cells[2])[:8] if len(cells) > 2 else []
            dx_activos = _clean_lines(cells[3])[:8] if len(cells) > 3 else []
            meds = _parse_meds(cells[4]) if len(cells) > 4 else []
            col_pend = cells[5] if len(cells) > 5 else ""
            col_obs = cells[6] if len(cells) > 6 else ""
            col_com = cells[7] if len(cells) > 7 else ""
            col_labs = cells[8] if len(cells) > 8 else ""

            pends = _parse_pendientes(cama_raw, col_pend, col_com)
            obs = _clean_lines(col_obs)[:6]
            labs: list[LaboratorioReciente] = []
            if col_labs.strip():
                labs.append(
                    LaboratorioReciente(
                        fecha_hora=datetime.now().strftime("%Y-%m-%d"),
                        tipo_estudio="Laboratorios / Gasometría",
                        valores={"resumen": re.sub(r"\s+", " ", col_labs).strip()[:350]},
                    )
                )

            pac = Paciente(
                cama=cama_raw,
                area=area,
                nombre_completo=nombre,
                hc=hc,
                cedula=ci,
                edad=edad,
                app=app,
                diagnosticos_activos=dx_activos,
                medicaciones=meds,
                pendientes=pends,
                observaciones=obs,
                laboratorios=labs,
            )
            save_cama(pac)
            total += 1

    consolidar_bitacora()
    return total


if __name__ == "__main__":
    n = sync_docx_to_bed_store(Path("/tmp/pacientes_activos.docx"))
    print(f"Sincronizadas {n} camas individuales desde /tmp/pacientes_activos.docx")
    res = generar_bitacora_completa()
    print("Quarto PDF generado:", res["pdf"], res["pdf"].stat().st_size, "bytes")
