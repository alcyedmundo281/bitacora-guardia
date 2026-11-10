"""Servidor Webhook HTTP para conectar el Grupo de WhatsApp con el Agente de Bitácora."""

import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from bitacora.models import Pendiente, PendientePrioridad, PendienteTipo, TurnoObjetivo
from bitacora.schema import load_bitacora, save_bitacora
from bitacora.whatsapp_bot import format_whatsapp_night_summary, parse_whatsapp_incoming_message

app = FastAPI(
    title="WhatsApp Webhook - Bitácora Médica",
    description="Bridge HTTP para sincronizar mensajes de grupos de WhatsApp con la bitácora.",
)


def get_active_bitacora_path() -> Path:
    """Busca la bitácora más reciente en la carpeta data/."""
    candidates = sorted(Path("data").glob("bitacora_*.json"))
    if candidates:
        return candidates[-1]
    return Path("data/bitacora_2026_11_09.json")


class WhatsAppIncomingPayload(BaseModel):
    """Payload estándar recibido desde puentes de WhatsApp (Evolution API / n8n / Twilio)."""

    sender: str | None = None
    group_id: str | None = None
    message: str


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "Bitacora WhatsApp Agent"}


@app.get("/whatsapp/resumen-nocturno")
def get_night_summary() -> dict[str, str]:
    """Retorna el resumen de la visita nocturna listo para emitir al grupo de WhatsApp."""
    bitacora_path = get_active_bitacora_path()
    if not bitacora_path.exists():
        raise HTTPException(status_code=404, detail="Bitácora no encontrada")
    bitacora = load_bitacora(bitacora_path)
    resumen = format_whatsapp_night_summary(bitacora)
    return {"resumen_texto": resumen}


@app.post("/whatsapp/webhook")
async def receive_whatsapp_message(request: Request) -> dict[str, Any]:
    """Punto de enlace (Webhook) que recibe los mensajes escritos en el grupo de WhatsApp."""
    body = await request.json()

    # Extraer el texto según el formato del bridge
    texto_mensaje = ""
    if "message" in body:
        texto_mensaje = str(body["message"])
    elif "text" in body:
        texto_mensaje = str(body["text"])
    elif "data" in body and "message" in body["data"]:
        texto_mensaje = str(body["data"]["message"])
    elif "entry" in body:
        try:
            texto_mensaje = body["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"]
        except (KeyError, IndexError):
            return {"status": "ignored", "reason": "No text message in payload"}
    else:
        texto_mensaje = str(body)

    # Limpiar menciones de WhatsApp (ej: @593999999999) y espacios
    texto_limpio = re.sub(r"^@\d+\s*", "", texto_mensaje).strip()
    cmd = texto_limpio.lower()
    bitacora_path = get_active_bitacora_path()

    # 1. Comandos directos de consulta en el grupo
    if cmd in ("!pendientes", "!guardia", "!resumen", "pendientes", "resumen"):
        bitacora = load_bitacora(bitacora_path)
        return {
            "status": "success",
            "tipo_accion": "CONSULTA_RESUMEN",
            "reply": format_whatsapp_night_summary(bitacora),
        }

    # 2. Comandos de ayuda y saludo
    if cmd in ("!ayuda", "!help", "ayuda", "help", "hola", "buenas", "bot", "menu", "!menu", "test", "hola bot"):
        return {
            "status": "success",
            "tipo_accion": "AYUDA",
            "reply": (
                "👋 *Asistente de Guardia Médica Activo (24/7)*\n\n"
                "📋 *Comandos de consulta:*\n"
                "• `!pendientes` o `!resumen` ➔ Checklist nocturno (STAT, urgentes y controles).\n"
                "• `!camas` ➔ Lista las camas censadas en el servicio.\n\n"
                "✍ *Registro durante el turno:*\n"
                "• `[Cama]: [Pendiente]` ➔ Registra un pendiente.\n"
                "   _Ejemplo:_ `6A: Gasometria STAT 21:30`\n"
                "• `[Cama] resultado: [Dato]` ➔ Registra y cierra el pendiente.\n"
                "   _Ejemplo:_ `6A resultado: Lactato 1.4 mmol/L normal`"
            ),
        }

    # 3. Listado de camas activas
    if cmd in ("!camas", "camas"):
        bitacora = load_bitacora(bitacora_path)
        camas_str = ", ".join(f"*{p.cama}* ({p.nombre_completo.split()[0]})" for p in bitacora.pacientes)
        return {
            "status": "success",
            "tipo_accion": "LISTA_CAMAS",
            "reply": f"🏥 *Camas activas censadas ({len(bitacora.pacientes)}):*\n{camas_str}",
        }

    # 4. Parseo de mensajes clínicos (ej. '6A: Gasometria STAT' o '6A resultado: Lactato 1.6')
    parsed = parse_whatsapp_incoming_message(texto_limpio)
    if not parsed:
        return {"status": "ignored", "reason": "No clinical pattern matched", "text": texto_limpio}

    cama = parsed["cama"]
    bitacora = load_bitacora(bitacora_path)
    paciente = next((p for p in bitacora.pacientes if p.cama.upper() == cama.upper()), None)
    if not paciente:
        camas_disponibles = ", ".join(p.cama for p in bitacora.pacientes[:8]) + "..."
        return {
            "status": "error",
            "reply": f"⚠ No se encontró paciente en la cama '{cama}' en la bitácora activa.\n_Camas disponibles:_ {camas_disponibles}",
        }

    if parsed["accion"] == "REGISTRAR_RESULTADO":
        resultado = parsed["resultado"]
        modificado = False
        pends_nuevos = []
        for pend in paciente.pendientes:
            if pend.es_visita_nocturna and pend.estado == "PENDIENTE" and not modificado:
                pends_nuevos.append(
                    pend.model_copy(
                        update={"estado": "COMPLETADO", "resultado_obtenido": resultado}
                    )
                )
                modificado = True
            else:
                pends_nuevos.append(pend)

        if not modificado:
            nuevo_p = Pendiente(
                id=f"PEND-{cama}-WA",
                tipo=PendienteTipo.LABORATORIO,
                descripcion="Reporte de guardia vía WhatsApp",
                prioridad=PendientePrioridad.STAT,
                turno=TurnoObjetivo.NOCTURNA_20_24,
                es_visita_nocturna=True,
                estado="COMPLETADO",
                resultado_obtenido=resultado,
            )
            pends_nuevos.append(nuevo_p)

        p_nuevo = paciente.model_copy(update={"pendientes": pends_nuevos})
        pacs_actualizados = [
            p_nuevo if p.cama.upper() == cama.upper() else p for p in bitacora.pacientes
        ]
        nueva_bit = bitacora.model_copy(update={"pacientes": pacs_actualizados})
        save_bitacora(nueva_bit, bitacora_path)

        return {
            "status": "success",
            "tipo_accion": "RESULTADO_REGISTRADO",
            "reply": f"✔ *Cama {cama}* ({paciente.nombre_completo.split()[0]}): Resultado documentado exitosamente ➔ `{resultado}`.",
        }

    elif parsed["accion"] == "CREAR_PENDIENTE":
        nuevo_id = f"PEND-{cama.upper()}-{len(paciente.pendientes) + 1:02d}"
        nuevo_pend = Pendiente(
            id=nuevo_id,
            tipo=parsed["tipo"],
            descripcion=parsed["descripcion"],
            prioridad=parsed["prioridad"],
            turno=parsed["turno"],
            horario_programado=parsed["horario"],
            es_visita_nocturna=parsed["es_nocturna"],
            estado="PENDIENTE",
        )
        nuevos_pends = list(paciente.pendientes) + [nuevo_pend]
        p_nuevo = paciente.model_copy(update={"pendientes": nuevos_pends})
        pacs_actualizados = [
            p_nuevo if p.cama.upper() == cama.upper() else p for p in bitacora.pacientes
        ]
        nueva_bit = bitacora.model_copy(update={"pacientes": pacs_actualizados})
        save_bitacora(nueva_bit, bitacora_path)

        return {
            "status": "success",
            "tipo_accion": "PENDIENTE_CREADO",
            "reply": f"📌 *Cama {cama}* ({paciente.nombre_completo.split()[0]}): Pendiente obligatorio registrado para la visita nocturna ➔ [{parsed['prioridad'].value}] {parsed['descripcion']}.",
        }

    return {"status": "unhandled"}
