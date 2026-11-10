"""Módulo de integración con WhatsApp para residentes y tratantes sin código."""

import re
from typing import Any

from bitacora.models import BitacoraGuardia, PendientePrioridad, PendienteTipo, TurnoObjetivo


def parse_whatsapp_incoming_message(text: str) -> dict[str, Any] | None:
    """Parsea mensajes informales de WhatsApp de los médicos para extraer pendientes o resultados.

    Ejemplos soportados:
    - '6A: Gasometria arterial STAT a las 21:30'
    - '8: Hemocultivo x2 urgente'
    - '6A resultado: EKG normal sin arritmia'
    """
    text = text.strip()
    if not text:
        return None

    # Caso 1: Reporte de resultado (ej. "6A resultado: ...")
    res_match = re.match(
        r"^([A-Za-z0-9\s-]+?)(?:\s+resultado|\s*:\s*resultado|\s*-\s*resultado)\s*:\s*(.+)$",
        text,
        re.IGNORECASE,
    )
    if res_match:
        cama = res_match.group(1).strip().upper()
        resultado = res_match.group(2).strip()
        return {
            "accion": "REGISTRAR_RESULTADO",
            "cama": cama,
            "resultado": resultado,
        }

    # Caso 2: Nuevo pendiente (ej. "6A: Gasometria arterial STAT 21:30")
    pend_match = re.match(r"^([A-Za-z0-9\s-]+?)\s*:\s*(.+)$", text)
    if pend_match:
        cama = pend_match.group(1).strip().upper()
        cuerpo = pend_match.group(2).strip()

        # Detección de prioridad por palabra clave
        prioridad = PendientePrioridad.RUTINA
        cuerpo_upper = cuerpo.upper()
        if "STAT" in cuerpo_upper or "INMEDIATO" in cuerpo_upper or "YA" in cuerpo_upper:
            prioridad = PendientePrioridad.STAT
        elif "URGENTE" in cuerpo_upper or "IMPORTANTE" in cuerpo_upper:
            prioridad = PendientePrioridad.URGENTE
        elif "CONTROL" in cuerpo_upper or "MADRUGADA" in cuerpo_upper or "05:00" in cuerpo:
            prioridad = PendientePrioridad.CONTROL

        # Detección de horario
        hora_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?|\d{1,2}h\d{0,2})", cuerpo)
        horario = hora_match.group(1) if hora_match else None

        # Detección de tipo
        tipo = PendienteTipo.LABORATORIO
        if any(w in cuerpo_upper for w in ["TAC", "RMN", "ECO", "EKG", "RX", "ENDOSCOPIA"]):
            tipo = PendienteTipo.IMAGEN_PROCEDIMIENTO
        elif any(
            w in cuerpo_upper for w in ["INTERCONSULTA", "VALORACION", "NEUMO", "CARDIO", "REUMA"]
        ):
            tipo = PendienteTipo.INTERCONSULTA
        elif any(w in cuerpo_upper for w in ["CURACION", "HERIDAS", "SONDA", "ASPIRACION"]):
            tipo = PendienteTipo.CUIDADO_ENFERMERIA
        elif any(w in cuerpo_upper for w in ["SANGRE", "CIRUGIA", "PAQUETES", "PLASMA"]):
            tipo = PendienteTipo.ADMINISTRATIVO_QX

        return {
            "accion": "CREAR_PENDIENTE",
            "cama": cama,
            "descripcion": cuerpo,
            "prioridad": prioridad,
            "tipo": tipo,
            "horario": horario,
            "turno": TurnoObjetivo.NOCTURNA_20_24,
            "es_nocturna": True,
        }

    return None


def format_whatsapp_night_summary(bitacora: BitacoraGuardia) -> str:
    """Genera el mensaje formateado con formato WhatsApp para el grupo de guardia."""
    lines = [
        f"🚨 *GUARDIA {bitacora.periodo}*",
        f"👨‍⚕️ *Equipo:* {', '.join(bitacora.medicos_guardia)}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🌙 *CHECKLIST DE LA VISITA NOCTURNA*",
        "",
    ]

    stat_pends = []
    urg_pends = []
    ctrl_pends = []

    for p in bitacora.pacientes:
        for pend in p.pendientes:
            if pend.es_visita_nocturna and pend.estado == "PENDIENTE":
                item_str = f"• *Cama {p.cama}* ({p.nombre_completo.split()[0]}): {pend.descripcion}"
                if pend.horario_programado:
                    item_str += f" ➔ _{pend.horario_programado}_"

                if pend.prioridad == PendientePrioridad.STAT:
                    stat_pends.append(item_str)
                elif pend.prioridad == PendientePrioridad.URGENTE:
                    urg_pends.append(item_str)
                else:
                    ctrl_pends.append(item_str)

    if stat_pends:
        lines.append("🔴 *CRÍTICOS / STAT:*")
        lines.extend(stat_pends)
        lines.append("")

    if urg_pends:
        lines.append("🟠 *URGENTES (20:00 - 24:00):*")
        lines.extend(urg_pends)
        lines.append("")

    if ctrl_pends:
        lines.append("🔵 *CONTROLES MADRUGADA (04:00 - 06:00 AM):*")
        lines.extend(ctrl_pends)
        lines.append("")

    if not (stat_pends or urg_pends or ctrl_pends):
        lines.append("✔ *No hay pendientes nocturnos activos.*")

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━━━",
            "💡 _Para reportar un resultado responde:_ `CAMA resultado: [DATO]`",
        ]
    )

    return "\n".join(lines)
