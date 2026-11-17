"""Sincroniza las matrices individuales de camas (data/camas/*.json) desde el Excel (.xlsx) de 9 columnas."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from openpyxl import load_workbook

from bitacora.bed_store import (
    META_FILE,
    archivar_camas_ausentes,
    consolidar_bitacora,
    save_cama,
)
from bitacora.excel_exporter import exportar_bitacora_xlsx
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


def _clean_lines(text: str | None) -> list[str]:
    if not text:
        return []
    return [
        re.sub(r"\s+", " ", line).strip(" •-\t")
        for line in str(text).splitlines()
        if line.strip(" •-\t\r\n")
    ]


def _clean_cama_name(raw_cama: str) -> str:
    # Quitar anotaciones entre paréntesis como '(Emergencia)', '(Periféricos)', '(Hospitalización ...)'
    base = re.sub(r"\s*\([^)]*\)", "", raw_cama).strip()
    base = re.sub(r"^Hab\.\s*", "Hab ", base, flags=re.IGNORECASE)
    return base.upper()


def _parse_patient_col(col2: str | None) -> tuple[str, str, str | None, str, int | None]:
    lines = _clean_lines(col2)
    nombre = lines[0].upper() if lines else "PACIENTE SIN NOMBRE"
    full = str(col2 or "")
    m_hc = re.search(r"HC:\s*(\d+)", full, re.IGNORECASE)
    hc = m_hc.group(1) if m_hc else "N/D"
    m_ci = re.search(r"CI:\s*(\d{8,10})", full, re.IGNORECASE)
    ci = m_ci.group(1) if m_ci else None
    m_edad = re.search(r"Edad:\s*([^\n(]+)", full, re.IGNORECASE)
    edad = m_edad.group(1).strip() if m_edad else "N/D"
    m_dias = re.search(r"D[ií]as de hospitalizaci[oó]n:\s*~?(\d+)", full, re.IGNORECASE)
    dias = int(m_dias.group(1)) if m_dias else None
    return nombre, hc, ci, edad, dias


def _parse_dx(col4: str | None) -> list[str]:
    out = []
    for line in _clean_lines(col4):
        cleaned = re.sub(r"^\d+\.\s*", "", line).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _parse_meds(col5: str | None) -> list[Medicacion]:
    meds = []
    for line in _clean_lines(col5):
        if line.upper().startswith("SUSPENDID"):
            meds.append(Medicacion(nombre=line[:140], dosis="", via="", frecuencia="", suspendido=True))
        else:
            meds.append(Medicacion(nombre=line[:140], dosis="", via="", frecuencia=""))
    return meds


def _parse_pendientes(cama: str, col6: str | None, col8: str | None) -> list[Pendiente]:
    pends: list[Pendiente] = []
    idx = 1
    for source_tipo, text in ((PendienteTipo.LABORATORIO, col6), (PendienteTipo.IMAGEN_PROCEDIMIENTO, col8)):
        for line in _clean_lines(text):
            up = line.upper()
            if "SIN PENDIENTES" in up or "SIN ESTUDIOS DE IMAGEN" in up:
                continue
            prio = PendientePrioridad.URGENTE
            if any(w in up for w in ("STAT", "INMEDIAT", "URGENTE", "HOY")):
                prio = PendientePrioridad.STAT if "STAT" in up else PendientePrioridad.URGENTE
            elif any(w in up for w in ("CONTROL", "DIARI", "SEGUIMIENTO")):
                prio = PendientePrioridad.CONTROL

            tipo = source_tipo
            if any(w in up for w in ("INTERCONSULTA", "IC ", "VALORACI", "CUIDADOS PALIATIVOS", "CLÍNICA DEL DOLOR")):
                tipo = PendienteTipo.INTERCONSULTA
            elif any(w in up for w in ("TAC", "RMN", "ECO", "RX", "UROTAC", "PET", "COLONOSCOPIA", "EDA")):
                tipo = PendienteTipo.IMAGEN_PROCEDIMIENTO

            pends.append(
                Pendiente(
                    id=f"PEND-{re.sub(r'[^A-Z0-9]', '', cama)}-{idx:02d}",
                    tipo=tipo,
                    descripcion=line[:220],
                    prioridad=prio,
                    turno=TurnoObjetivo.NOCTURNA_20_24,
                    es_visita_nocturna=True,
                    estado="PENDIENTE",
                )
            )
            idx += 1
    return pends


def sync_xlsx_to_bed_store(xlsx_path: Path) -> int:
    wb = load_workbook(str(xlsx_path))
    ws = wb.active

    # Fila 2: CENSO Y EQUIPO
    c2_lines = _clean_lines(ws.cell(2, 2).value)
    periodo = datetime.now().strftime("%d/%m/%Y")
    medicos: list[str] = []
    for ln in c2_lines:
        if "BITÁCORA" in ln.upper():
            m = re.search(r"(\d{2}/\d{2}/\d{4}.*)", ln)
            if m:
                periodo = m.group(1).strip()
        else:
            medicos.append(ln)

    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    META_FILE.write_text(
        json.dumps(
            {
                "version_schema": "1.0.0",
                "fecha_guardia": datetime.now().strftime("%Y-%m-%d"),
                "periodo": periodo,
                "medicos_guardia": medicos or ["Equipo de Guardia"],
                "enlaces_utiles": {},
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    area: Literal["HOSPITALIZACION", "PERIFERICOS", "EMERGENCIA_OBSERVACION"] = "HOSPITALIZACION"
    seen_beds: dict[str, int] = {}
    total = 0

    for r in range(3, ws.max_row + 1):
        c1_raw = str(ws.cell(r, 1).value or "").strip()
        if not c1_raw:
            continue
        up = c1_raw.upper()
        if up.startswith("SECCIÓN I"):
            area = "HOSPITALIZACION"
            continue
        if up.startswith("SECCIÓN II"):
            area = "PERIFERICOS"
            continue
        if up.startswith("SECCIÓN III"):
            area = "EMERGENCIA_OBSERVACION"
            continue

        cama = _clean_cama_name(c1_raw)
        if cama in seen_beds:
            seen_beds[cama] += 1
            cama = f"{cama}_{seen_beds[cama]}"
        else:
            seen_beds[cama] = 1

        nombre, hc, ci, edad, dias = _parse_patient_col(ws.cell(r, 2).value)
        app = _clean_lines(ws.cell(r, 3).value)
        dx = _parse_dx(ws.cell(r, 4).value)
        meds = _parse_meds(ws.cell(r, 5).value)
        pends = _parse_pendientes(cama, ws.cell(r, 6).value, ws.cell(r, 8).value)

        labs: list[LaboratorioReciente] = []
        for lab_line in _clean_lines(ws.cell(r, 7).value):
            m_fecha = re.match(r"^(\d{2}/\d{2}/\d{4}(?:\s*\([^)]+\))?)\s*:\s*(.+)$", lab_line)
            fecha_h = m_fecha.group(1) if m_fecha else datetime.now().strftime("%d/%m/%Y")
            resumen = m_fecha.group(2) if m_fecha else lab_line
            labs.append(
                LaboratorioReciente(
                    fecha_hora=fecha_h,
                    tipo_estudio="Laboratorio",
                    valores={"resumen": resumen[:400]},
                )
            )

        obs = _clean_lines(ws.cell(r, 9).value)
        alertas = [o for o in obs if any(k in o.upper() for k in ("IMPORTANTE", "NO RCP", "LET", "ALERG", "CRÍTIC", "VIGILAR"))]

        pac = Paciente(
            cama=cama,
            area=area,
            nombre_completo=nombre,
            hc=hc,
            cedula=ci,
            edad=edad,
            dias_hospitalizacion=dias,
            app=app,
            diagnosticos_activos=dx,
            medicaciones=meds,
            pendientes=pends,
            observaciones=obs,
            alertas_criticas=alertas[:3],
            laboratorios=labs,
        )
        save_cama(pac)
        total += 1

    archivadas = archivar_camas_ausentes(set(seen_beds.keys()))
    if archivadas:
        print(f"Camas archivadas por egreso/ausencia ({len(archivadas)}): {', '.join(archivadas)}")
    consolidar_bitacora()
    return total


if __name__ == "__main__":
    n = sync_xlsx_to_bed_store(Path("/tmp/bitacora_hoy.xlsx"))
    print(f"Sincronizadas {n} camas individuales desde /tmp/bitacora_hoy.xlsx")
    res_pdf = generar_bitacora_completa()
    print("Quarto PDF generado:", res_pdf["pdf"], res_pdf["pdf"].stat().st_size, "bytes")
    xlsx_out = exportar_bitacora_xlsx()
    print("Excel XLSX generado:", xlsx_out, xlsx_out.stat().st_size, "bytes")
