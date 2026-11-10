"""Sincronizador bidireccional entre la Bitácora y una Grilla de Google Sheets / Excel."""

import csv
from pathlib import Path

from bitacora.models import (
    BitacoraGuardia,
    Pendiente,
    PendientePrioridad,
    PendienteTipo,
    TurnoObjetivo,
)
from bitacora.schema import load_bitacora, save_bitacora

HEADERS_GRILLA = [
    "CAMA",
    "PACIENTE",
    "HISTORIA_CLINICA",
    "DIAGNOSTICOS_ACTIVOS",
    "ANTIBIOTICOS_Y_DIAS",
    "ID_PENDIENTE",
    "TIPO_PENDIENTE",
    "DESCRIPCION_PENDIENTE",
    "PRIORIDAD",
    "HORARIO_PROGRAMADO",
    "ES_VISITA_NOCTURNA",
    "ESTADO",
    "RESULTADO_OBTENIDO",
]


def export_to_editable_grid(bitacora_path: Path, csv_output_path: Path) -> Path:
    """Exporta los pacientes y pendientes a una grilla CSV/Google Sheets fácil de editar sin código."""
    bitacora = load_bitacora(bitacora_path)
    rows: list[list[str]] = []

    for p in bitacora.pacientes:
        dx_str = "; ".join(p.diagnosticos_activos)
        atb_list = [
            f"{m.nombre} ({m.dias_tratamiento})" for m in p.medicaciones if m.dias_tratamiento
        ]
        atb_str = "; ".join(atb_list)

        if not p.pendientes:
            rows.append(
                [
                    p.cama,
                    p.nombre_completo,
                    p.hc,
                    dx_str,
                    atb_str,
                    "",
                    "",
                    "Sin pendientes activos",
                    "RUTINA",
                    "",
                    "NO",
                    "COMPLETADO",
                    "",
                ]
            )
        else:
            for pend in p.pendientes:
                rows.append(
                    [
                        p.cama,
                        p.nombre_completo,
                        p.hc,
                        dx_str,
                        atb_str,
                        pend.id,
                        pend.tipo.value,
                        pend.descripcion,
                        pend.prioridad.value,
                        pend.horario_programado or "",
                        "SI" if pend.es_visita_nocturna else "NO",
                        pend.estado,
                        pend.resultado_obtenido or "",
                    ]
                )

    csv_output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_output_path, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS_GRILLA)
        writer.writerows(rows)

    return csv_output_path


def import_from_editable_grid(
    csv_input_path: Path, bitacora_base_path: Path, output_path: Path
) -> BitacoraGuardia:
    """Lee la grilla modificada por los médicos en Drive y actualiza el JSON canónico."""
    bitacora_base = load_bitacora(bitacora_base_path)

    pendientes_por_cama: dict[str, list[Pendiente]] = {}

    with open(csv_input_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            cama = (row.get("CAMA") or "").strip().upper()
            desc = (row.get("DESCRIPCION_PENDIENTE") or "").strip()
            if not cama or not desc or desc == "Sin pendientes activos":
                continue

            pend_id = (row.get("ID_PENDIENTE") or "").strip() or f"PEND-{cama}-{idx:02d}"
            tipo_str = (row.get("TIPO_PENDIENTE") or "LABORATORIO").strip().upper()
            prio_str = (row.get("PRIORIDAD") or "RUTINA").strip().upper()
            horario = (row.get("HORARIO_PROGRAMADO") or "").strip() or None
            es_nocturna = (row.get("ES_VISITA_NOCTURNA") or "NO").strip().upper() in (
                "SI",
                "S",
                "TRUE",
                "1",
            )
            estado = (row.get("ESTADO") or "PENDIENTE").strip().upper()
            if estado not in ("PENDIENTE", "EN_CURSO", "COMPLETADO", "CANCELADO"):
                estado = "PENDIENTE"
            resultado = (row.get("RESULTADO_OBTENIDO") or "").strip() or None

            tipo = getattr(PendienteTipo, tipo_str, PendienteTipo.LABORATORIO)
            prio = getattr(PendientePrioridad, prio_str, PendientePrioridad.RUTINA)

            pend = Pendiente(
                id=pend_id,
                tipo=tipo,
                descripcion=desc,
                prioridad=prio,
                turno=TurnoObjetivo.NOCTURNA_20_24 if es_nocturna else TurnoObjetivo.MANANA,
                horario_programado=horario,
                es_visita_nocturna=es_nocturna,
                estado=estado,  # type: ignore[arg-type]
                resultado_obtenido=resultado,
            )

            if cama not in pendientes_por_cama:
                pendientes_por_cama[cama] = []
            pendientes_por_cama[cama].append(pend)

    # Actualizar pacientes
    nuevos_pacientes = []
    for p in bitacora_base.pacientes:
        cama_key = p.cama.upper()
        if cama_key in pendientes_por_cama:
            p_actualizado = p.model_copy(update={"pendientes": pendientes_por_cama[cama_key]})
            nuevos_pacientes.append(p_actualizado)
        else:
            nuevos_pacientes.append(p)

    nueva_bitacora = bitacora_base.model_copy(update={"pacientes": nuevos_pacientes})
    save_bitacora(nueva_bitacora, output_path)
    return nueva_bitacora
