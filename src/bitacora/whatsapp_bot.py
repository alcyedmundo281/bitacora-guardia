"""Módulo de integración con WhatsApp para residentes y tratantes sin código."""

import re
from typing import Any

from bitacora.models import BitacoraGuardia, PendientePrioridad, PendienteTipo, TurnoObjetivo

# Regex representativo para las camas clínicas del hospital
BED_REGEX = (
    r"(?:INFECTO\s+1B|CARDIO\s+\d+[A-Za-z]|NEUMO\s+[A-Za-z0-9]+|UROLOGIA\s+[A-Za-z]+|"
    r"INTERMEDIOS\s+[A-Za-z]+|ER-AB\s+\d+|ER\s+[A-Za-z]+|ABREVIADOS\s+[A-Za-z]+|"
    r"AB\s+\d+[A-Za-z]|\d{1,2}[A-Za-z]|\d{1,2})"
)


def strip_robotina(text: str) -> str:
    """Remueve invocaciones al bot (ej: Robotina, Robotina:, @Robotina) del mensaje."""
    t = text.strip()
    return re.sub(
        r"^(?:@?[Rr]obotina\b[:,\s-]*|@?\w+\s+robotina\b[:,\s-]*)",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()


def parse_whatsapp_incoming_message(text: str) -> dict[str, Any] | None:
    """Parsea mensajes de WhatsApp de los médicos para extraer pendientes o resultados.

    Soporta formato con o sin Robotina, lenguaje natural o comandos estructurados:
    - 'Robotina la gasometria de 6a es pf 6.9'
    - 'Robotina, la gasometria de 6a es pf 6.9'
    - 'la gasometria de 6a es pf 6.9'
    - '6A resultado: Lactato 1.4 mmol/L y gases normales'
    - '6A: Gasometria arterial STAT 21:30'
    - 'el ekg de 6d reporta ritmo sinusal'
    - 'troponina de 6d salio negativa'
    - 'Robotina como esta la 6a'
    """
    cleaned = strip_robotina(text)
    if not cleaned:
        # Si el usuario solo escribió "Robotina" o "Hola Robotina"
        return {"accion": "SALUDO_ROBOTINA"}

    # 1. Consulta de estado de una cama específica (ej: 'como esta la 6a', 'paciente 6a', '6a')
    m_cama_query = re.match(
        r"^(?:c[oó]mo\s+est[aá](?:\s+la)?|paciente(?:\s+de\s+la)?|estado(?:\s+de\s+la)?|ver|info)?\s*("
        + BED_REGEX
        + r")$",
        cleaned,
        re.IGNORECASE,
    )
    if m_cama_query:
        return {"accion": "CONSULTAR_CAMA", "cama": m_cama_query.group(1).upper()}

    # 2. Formato estructurado de resultado: '6A resultado: ...' o 'resultado 6A: ...'
    res_match = re.match(
        r"^(" + BED_REGEX + r")(?:\s+resultado|\s*:\s*resultado|\s*-\s*resultado)\s*:\s*(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if res_match:
        cama = res_match.group(1).strip().upper()
        resultado = res_match.group(2).strip()
        return {
            "accion": "REGISTRAR_RESULTADO",
            "cama": cama,
            "estudio": None,
            "resultado": resultado,
            "valor": resultado,
        }

    # 3. Lenguaje natural: '[estudio] de/en [cama] (es|salio|reporta|dio|:) [resultado]'
    # Ej: 'la gasometria de 6a es pf 6.9' o 'el ekg de 6d reporta ritmo sinusal'
    nat_res1 = re.search(
        r"^(.*?)\s*(?:de\s+la|en\s+la|de|en|cama)\s+(" + BED_REGEX + r")\s*(?:es|salio|salió|reporta|dio|con|:)\s*(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if nat_res1:
        estudio_raw = nat_res1.group(1).strip()
        estudio = re.sub(r"^(el|la|los|las)\s+", "", estudio_raw, flags=re.IGNORECASE).strip()
        cama = nat_res1.group(2).upper()
        val = nat_res1.group(3).strip()
        val = re.sub(r"^(es|salio|salió|reporta|dio|con|:)\s*", "", val, flags=re.IGNORECASE).strip()
        resultado_fmt = f"{estudio.capitalize()}: {val}" if estudio else val
        return {
            "accion": "REGISTRAR_RESULTADO",
            "cama": cama,
            "estudio": estudio,
            "resultado": resultado_fmt,
            "valor": val,
        }

    # 4. Lenguaje natural inverso: 'en [cama] [estudio] es [resultado]'
    # Ej: 'en la 6a la gasometria es pf 6.9'
    nat_res2 = re.search(
        r"^(?:en\s+la|de\s+la|en|de|cama)\s+(" + BED_REGEX + r")\s+(?:la|el|los|las)?\s*(.*?)\s*(?:es|salio|salió|reporta|dio|:)\s*(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if nat_res2:
        cama = nat_res2.group(1).upper()
        estudio_raw = nat_res2.group(2).strip()
        estudio = re.sub(r"^(el|la|los|las)\s+", "", estudio_raw, flags=re.IGNORECASE).strip()
        val = nat_res2.group(3).strip()
        val = re.sub(r"^(es|salio|salió|reporta|dio|con|:)\s*", "", val, flags=re.IGNORECASE).strip()
        resultado_fmt = f"{estudio.capitalize()}: {val}" if estudio else val
        return {
            "accion": "REGISTRAR_RESULTADO",
            "cama": cama,
            "estudio": estudio,
            "resultado": resultado_fmt,
            "valor": val,
        }

    # 5. Nuevo pendiente estructurado (ej. '6A: Gasometria arterial STAT 21:30' o 'anotar en 6A: ...')
    pend_match = re.match(
        r"^(?:anotar\s+en\s+|agregar\s+en\s+|pendiente\s+para\s+)?(" + BED_REGEX + r")\s*:\s*(.+)$",
        cleaned,
        re.IGNORECASE,
    )
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
            "💡 _Para reportar un resultado escribe:_ `Robotina la gasometria de 6a es pf 6.9`",
        ]
    )

    return "\n".join(lines)
