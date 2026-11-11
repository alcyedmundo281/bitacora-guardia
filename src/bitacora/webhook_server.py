import asyncio
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from bitacora.groq_agent import call_groq_clinical_parser, set_groq_api_key, test_groq_connection
from bitacora.models import Paciente, Pendiente, PendientePrioridad, PendienteTipo, TurnoObjetivo
from bitacora.pdf_generator import generar_bitacora_completa
from bitacora.reminders import (
    crear_recordatorio,
    emitir_alerta_whatsapp,
    formatear_recordatorios,
    parse_hora_recordatorio,
    verificar_recordatorios_a_disparar,
)
from bitacora.schema import load_bitacora, save_bitacora
from bitacora.whatsapp_bot import (
    BED_REGEX,
    format_whatsapp_night_summary,
    parse_whatsapp_incoming_message,
    strip_robotina,
)

logger = logging.getLogger(__name__)


def normalize_text(s: str) -> str:
    """Normaliza texto eliminando acentos/tildes y convirtiendo a minúsculas."""
    nfkd = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

app = FastAPI(
    title="WhatsApp Webhook - Bitácora Médica & Robotina",
    description="Bridge HTTP para sincronizar mensajes de grupos de WhatsApp con la bitácora médica.",
)


@app.on_event("startup")
async def startup_event():
    """Inicia el worker en segundo plano para verificar alarmas y recordatorios clínicos."""
    async def reminder_loop():
        while True:
            try:
                alertas = verificar_recordatorios_a_disparar()
                for al in alertas:
                    emitir_alerta_whatsapp(al)
            except Exception as e:
                logger.error(f"Error en worker de recordatorios: {e}")
            await asyncio.sleep(25)

    asyncio.create_task(reminder_loop())


def get_active_bitacora_path() -> Path:
    """Busca la bitácora más reciente en la carpeta data/."""
    candidates = sorted(Path("data").glob("bitacora_*.json"))
    if candidates:
        return candidates[-1]
    return Path("data/bitacora_2026_11_09.json")


def format_paciente_card(pac: Paciente) -> str:
    """Genera la ficha clínica completa de una cama/paciente para WhatsApp."""
    dx = pac.diagnosticos_activos[0] if pac.diagnosticos_activos else "Sin diagnóstico activo"
    lines = [
        f"🏥 *CAMA {pac.cama}: {pac.nombre_completo}* ({pac.edad} años)",
        f"🩺 *Dx:* {dx}",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if pac.alertas_criticas:
        lines.append("⚠ *Alertas Clínicas:*")
        for al in pac.alertas_criticas:
            lines.append(f"• {al}")
        lines.append("")

    if pac.laboratorios:
        lines.append("🔬 *Últimos Exámenes / Laboratorios:*")
        for lab in pac.laboratorios:
            vals_str = ", ".join(f"{k.capitalize()}: {v}" for k, v in lab.valores.items())
            lines.append(f"• *{lab.tipo_estudio}* ({lab.fecha_hora}): {vals_str}")
        lines.append("")

    lines.append("📋 *Pendientes de la Guardia:*")
    if not pac.pendientes:
        lines.append("• _Sin pendientes registrados para este paciente._")
    else:
        for pend in pac.pendientes:
            icon = "✅" if pend.estado == "COMPLETADO" else ("🔴" if pend.prioridad == PendientePrioridad.STAT else "🟠")
            res_str = f" ➔ _{pend.resultado_obtenido}_" if pend.resultado_obtenido else ""
            lines.append(f"• {icon} [{pend.estado}] {pend.descripcion}{res_str}")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💡 _Para reportar resultado:_ `Robotina la gasometria de {pac.cama} es pf ...`")
    return "\n".join(lines)


class WhatsAppIncomingPayload(BaseModel):
    """Payload estándar recibido desde puentes de WhatsApp."""

    sender: str | None = None
    group_id: str | None = None
    message: str


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "Bitacora WhatsApp Agent (Robotina)"}


@app.get("/whatsapp/resumen-nocturno")
def get_night_summary() -> dict[str, str]:
    """Retorna el resumen de la visita nocturna listo para emitir al grupo de WhatsApp."""
    bitacora_path = get_active_bitacora_path()
    if not bitacora_path.exists():
        raise HTTPException(status_code=404, detail="Bitácora no encontrada")
    bitacora = load_bitacora(bitacora_path)
    resumen = format_whatsapp_night_summary(bitacora)
    return {"resumen_texto": resumen}


@app.get("/descargar/bitacora.pdf")
def download_pdf() -> FileResponse:
    """Descarga directa del PDF de la bitácora médica compilado por Quarto."""
    pdf_path = Path("reports/bitacora_guardia.pdf")
    if not pdf_path.exists():
        bitacora_path = get_active_bitacora_path()
        generar_bitacora_completa(bitacora_path)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="El PDF aún no ha sido generado.")
    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        filename="bitacora_guardia.pdf",
    )


@app.get("/descargar/bitacora.html")
def download_html() -> FileResponse:
    """Visualización directa del dashboard interactivo HTML de Quarto."""
    html_path = Path("reports/bitacora_guardia.html")
    if not html_path.exists():
        bitacora_path = get_active_bitacora_path()
        generar_bitacora_completa(bitacora_path)
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="El HTML aún no ha sido generado.")
    return FileResponse(
        str(html_path),
        media_type="text/html",
        filename="bitacora_guardia.html",
    )


class SetGroqKeyPayload(BaseModel):
    key: str | None = None
    groq_api_key: str | None = None


@app.get("/debug/groq")
def debug_groq() -> dict[str, Any]:
    """Diagnóstico en vivo de la conexión con Groq."""
    return test_groq_connection()


@app.post("/api/set-groq-key")
def set_groq_key_endpoint(payload: SetGroqKeyPayload) -> dict[str, Any]:
    """Guarda la clave de Groq dinámicamente y la prueba de inmediato."""
    k = payload.key or payload.groq_api_key
    if not k:
        raise HTTPException(status_code=400, detail="Se requiere 'key' o 'groq_api_key'")
    set_groq_api_key(k)
    diag = test_groq_connection(k)
    return {"status": "ok", "diagnostico": diag}


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

    chat_id = str(body.get("group_id") or body.get("sender") or "")

    # Limpiar menciones de números telefónicos de WhatsApp (ej: @593999999999)
    texto_limpio = re.sub(r"^@\d+\s*", "", texto_mensaje).strip()
    texto_sin_robotina = strip_robotina(texto_limpio)
    cmd = texto_sin_robotina.lower()
    bitacora_path = get_active_bitacora_path()
    bitacora = load_bitacora(bitacora_path)

    # 1. INTELIGENCIA CLÍNICA GROQ (Gemma / Llama 3)
    # Preparamos el contexto actual de pacientes para el LLM
    camas_info = [
        {
            "cama": p.cama,
            "nombre": p.nombre_completo,
            "dx": p.diagnosticos_activos[0] if p.diagnosticos_activos else "",
            "pendientes": ", ".join(f"{pend.descripcion} [{pend.estado}]" for pend in p.pendientes),
        }
        for p in bitacora.pacientes
    ]

    groq_res = call_groq_clinical_parser(texto_limpio, camas_info)
    if groq_res and isinstance(groq_res, dict) and "intencion" in groq_res:
        intencion = groq_res["intencion"]

        if intencion == "CONSULTAR_RESUMEN":
            return {
                "status": "success",
                "tipo_accion": "CONSULTA_RESUMEN",
                "reply": format_whatsapp_night_summary(bitacora),
            }

        if intencion == "CONSULTAR_CAMAS":
            camas_str = ", ".join(f"*{p.cama}* ({p.nombre_completo.split()[0]})" for p in bitacora.pacientes)
            return {
                "status": "success",
                "tipo_accion": "LISTA_CAMAS",
                "reply": f"🏥 *Camas activas censadas ({len(bitacora.pacientes)}):*\n{camas_str}\n\n_Para ver una cama escribe:_ `Robotina como esta 6A`",
            }

        if intencion == "GENERAR_PDF":
            generar_bitacora_completa(bitacora_path)
            return {
                "status": "success",
                "tipo_accion": "GENERAR_PDF",
                "reply": (
                    "📄 *Bitácora Médica Oficial Generada con Quarto (PDF & Web)*\n\n"
                    f"🏥 *Periodo:* {bitacora.periodo}\n"
                    f"👥 *Total Pacientes:* {len(bitacora.pacientes)} censados\n"
                    f"🌙 *Checklist nocturno:* Actualizado con las novedades reportadas por WhatsApp.\n\n"
                    "📥 *Descargar PDF para Entrega de Guardia:*\n"
                    "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.pdf\n\n"
                    "🌐 *Ver reporte interactivo en línea:*\n"
                    "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.html"
                ),
            }

        if intencion == "CREAR_RECORDATORIO":
            cama_target = str(groq_res.get("cama") or "").upper()
            pac = next((p for p in bitacora.pacientes if p.cama.upper() == cama_target), None)
            pac_nombre = pac.nombre_completo if pac else (groq_res.get("paciente") or "Paciente de guardia")
            cama_display = pac.cama if pac else (cama_target or "Guardia")
            hora_raw = groq_res.get("horario") or "07:00"
            tarea = groq_res.get("estudio") or groq_res.get("tarea") or "Control clínico"

            rec = crear_recordatorio(hora_raw, cama_display, pac_nombre, tarea, chat_id=chat_id)
            return {
                "status": "success",
                "tipo_accion": "CREAR_RECORDATORIO",
                "reply": (
                    f"⏰ *Recordatorio programado por Robotina:*\n"
                    f"🏥 *Cama {rec['cama']}:* {rec['paciente']}\n"
                    f"📋 *Acción:* {rec['tarea']}\n"
                    f"🕒 *Hora programada:* `{rec['hora_programada']}`\n\n"
                    f"🔔 _Robotina emitirá una alerta automática en este grupo al llegar la hora._"
                ),
            }

        if intencion == "CONSULTAR_RECORDATORIOS":
            return {
                "status": "success",
                "tipo_accion": "LISTA_RECORDATORIOS",
                "reply": formatear_recordatorios(),
            }

        if intencion == "AYUDA":
            return {
                "status": "success",
                "tipo_accion": "AYUDA",
                "reply": (
                    "🤖 *¡Hola! Soy Robotina, asistente clínica con IA (Groq 24/7)*\n\n"
                    "Puedes hablarme con total lenguaje natural sin comandos rígidos:\n\n"
                    "• *Reportar resultados:* `Robotina la gaso de 6a dio ph 6.9 y lactato 3`\n"
                    "• *Consultar paciente o cama:* `Robotina cómo sigue don Rodriguez?` o `Robotina 6a`\n"
                    "• *Checklist nocturno:* `Robotina qué pendientes críticos quedan?`\n"
                    "• *Anotar tareas:* `Robotina anota hemocultivos urgente al de la 8 para las 23:00`"
                ),
            }

        if intencion == "CONSULTAR_CAMA":
            cama_target = str(groq_res.get("cama") or "").upper()
            pac = next((p for p in bitacora.pacientes if p.cama.upper() == cama_target), None)
            if not pac and groq_res.get("paciente"):
                p_query = str(groq_res["paciente"]).lower()
                for p in bitacora.pacientes:
                    if any(tok in p.nombre_completo.lower() for tok in p_query.split() if len(tok) > 3):
                        pac = p
                        break
            if pac:
                return {
                    "status": "success",
                    "tipo_accion": "CONSULTA_CAMA",
                    "reply": format_paciente_card(pac),
                }

        if intencion == "REGISTRAR_RESULTADO" and groq_res.get("cama"):
            cama_target = str(groq_res["cama"]).upper()
            pac = next((p for p in bitacora.pacientes if p.cama.upper() == cama_target), None)
            if pac:
                estudio = groq_res.get("estudio") or "Reporte"
                val = groq_res.get("resultado") or ""
                alerta = groq_res.get("comentario_clinico")
                modificado = False
                pend_desc = ""
                pends_nuevos = []

                for pend in pac.pendientes:
                    coincide = bool(estudio.lower() in pend.descripcion.lower() or (estudio.lower().startswith("gas") and "gas" in pend.descripcion.lower()))
                    if pend.es_visita_nocturna and pend.estado == "PENDIENTE" and (coincide or not modificado):
                        pends_nuevos.append(
                            pend.model_copy(update={"estado": "COMPLETADO", "resultado_obtenido": val})
                        )
                        modificado = True
                        pend_desc = pend.descripcion
                    else:
                        pends_nuevos.append(pend)

                if not modificado:
                    nuevo_desc = f"{estudio} (WhatsApp)"
                    nuevo_p = Pendiente(
                        id=f"PEND-{cama_target}-GROQ",
                        tipo=PendienteTipo.LABORATORIO,
                        descripcion=nuevo_desc,
                        prioridad=PendientePrioridad.STAT,
                        turno=TurnoObjetivo.NOCTURNA_20_24,
                        es_visita_nocturna=True,
                        estado="COMPLETADO",
                        resultado_obtenido=val,
                    )
                    pends_nuevos.append(nuevo_p)
                    pend_desc = nuevo_desc

                p_nuevo = pac.model_copy(update={"pendientes": pends_nuevos})
                pacs_actualizados = [
                    p_nuevo if p.cama.upper() == cama_target else p for p in bitacora.pacientes
                ]
                nueva_bit = bitacora.model_copy(update={"pacientes": pacs_actualizados})
                save_bitacora(nueva_bit, bitacora_path)

                reply_msg = (
                    f"✔ *Resultado registrado por Robotina (IA):*\n"
                    f"🏥 *Cama {cama_target}:* {pac.nombre_completo.split()[0]} {pac.nombre_completo.split()[-1] if len(pac.nombre_completo.split()) > 1 else ''}\n"
                    f"📋 *Estudio:* {pend_desc}\n"
                    f"📊 *Resultado:* `{val}`\n"
                    f"✅ *Estado:* COMPLETADO"
                )
                if alerta:
                    reply_msg += f"\n⚠ *Nota clínica:* {alerta}"

                return {"status": "success", "tipo_accion": "RESULTADO_REGISTRADO", "reply": reply_msg}

    # 2. MOTOR DE RESPALDO (REGLAS Y NLP LOCAL SI GROQ ESTÁ EN TIMEOUT O FUERA DE LÍNEA)
    # Comandos de consulta de resumen / checklist nocturno
    if cmd in ("!pendientes", "!guardia", "!resumen", "pendientes", "resumen", "guardia") or any(
        w in cmd for w in ["que pendientes hay", "que hay de pendientes", "que hay pendiente", "como van los pendientes", "checklist"]
    ):
        return {
            "status": "success",
            "tipo_accion": "CONSULTA_RESUMEN",
            "reply": format_whatsapp_night_summary(bitacora),
        }

    # Comandos para generar y descargar PDF de la bitácora con Quarto
    if cmd in ("!pdf", "pdf", "!reporte", "reporte", "descargar pdf", "generar pdf", "entrega de guardia", "bitacora pdf") or any(
        w in cmd for w in ["genera el pdf", "generar el pdf", "hacer el pdf", "descargar el pdf", "entrega de guardia", "bitacora en pdf", "quarto", "reporte pdf", "descargar bitacora", "genera la bitacora", "generar la bitacora"]
    ):
        generar_bitacora_completa(bitacora_path)
        return {
            "status": "success",
            "tipo_accion": "GENERAR_PDF",
            "reply": (
                "📄 *Bitácora Médica Oficial Generada con Quarto (PDF & Web)*\n\n"
                f"🏥 *Periodo:* {bitacora.periodo}\n"
                f"👥 *Total Pacientes:* {len(bitacora.pacientes)} censados\n"
                f"🌙 *Checklist nocturno:* Actualizado con las novedades reportadas por WhatsApp.\n\n"
                "📥 *Descargar PDF para Entrega de Guardia:*\n"
                "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.pdf\n\n"
                "🌐 *Ver reporte interactivo en línea:*\n"
                "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.html"
            ),
        }

    # Comandos para consultar recordatorios activos
    if cmd in ("!recordatorios", "recordatorios", "alarmas", "!alarmas") or any(
        w in cmd for w in ["que recordatorios hay", "lista de recordatorios", "alarmas activas"]
    ):
        return {
            "status": "success",
            "tipo_accion": "LISTA_RECORDATORIOS",
            "reply": formatear_recordatorios(),
        }

    # Creación de recordatorio en lenguaje natural
    # Soporta: 'recuerdanos reportar sodio para 8a a las 7pm' y 'recuerdanos a las 7 el sodio de rodriguez'
    m_rec = re.search(
        r"(?:recu[eé]rda(?:nos|me)?|alarma|avisa(?:nos|me)?)\s+(.+)",
        cmd,
        re.IGNORECASE,
    )
    if m_rec:
        contenido = m_rec.group(1).strip()
        m_hora = re.search(
            r"(?:a\s+las\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)|(\d{1,2}:\d{2}\s*(?:am|pm)?)|(\d{1,2}\s*(?:am|pm)))",
            contenido,
            re.IGNORECASE,
        )
        if m_hora:
            hora_str = m_hora.group(1) or m_hora.group(2) or m_hora.group(3)
            resto = (contenido[:m_hora.start()] + " " + contenido[m_hora.end():]).strip()
        else:
            hora_str = "07:00"
            resto = contenido

        # Buscar paciente por cama o apellido en el resto del texto
        paciente_match = None
        resto_norm = normalize_text(resto)
        for p in bitacora.pacientes:
            if re.search(r"\b" + re.escape(normalize_text(p.cama)) + r"\b", resto_norm):
                paciente_match = p
                break
            for n in normalize_text(p.nombre_completo).split():
                if len(n) > 3 and re.search(r"\b" + re.escape(n) + r"\b", resto_norm):
                    paciente_match = p
                    break
            if paciente_match:
                break

        m_cama_direct = re.search(r"\b(" + BED_REGEX + r")\b", resto, re.IGNORECASE)
        cama_rec = paciente_match.cama if paciente_match else (m_cama_direct.group(1).upper() if m_cama_direct else "Guardia")
        nombre_rec = paciente_match.nombre_completo if paciente_match else "Paciente asignado"
        tarea_rec = resto
        tarea_rec = re.sub(r"(?:para\s+la|para\s+el|para|de\s+la|del?\s+)?\b(?:" + BED_REGEX + r")\b", "", tarea_rec, flags=re.IGNORECASE).strip()
        tarea_rec = re.sub(r"(?:del\s+)?(?:sr\.?|don|doña|paciente)?\s*[a-zA-Z0-9]+$", "", tarea_rec, flags=re.IGNORECASE).strip()
        if not tarea_rec:
            tarea_rec = resto.capitalize()
        else:
            tarea_rec = tarea_rec.capitalize()

        rec = crear_recordatorio(hora_str, cama_rec, nombre_rec, tarea_rec, chat_id=chat_id)
        return {
            "status": "success",
            "tipo_accion": "CREAR_RECORDATORIO",
            "reply": (
                f"⏰ *Recordatorio programado por Robotina:*\n"
                f"🏥 *Cama {rec['cama']}:* {rec['paciente']}\n"
                f"📋 *Acción:* {rec['tarea']}\n"
                f"🕒 *Hora programada:* `{rec['hora_programada']}`\n\n"
                f"🔔 _Robotina emitirá una alerta automática en este grupo al llegar la hora._"
            ),
        }

    # Comandos de ayuda y saludo a Robotina
    if cmd in ("!ayuda", "!help", "ayuda", "help", "hola", "buenas", "bot", "menu", "!menu", "test", "hola bot", "robotina") or any(
        w in cmd for w in ["quien eres", "que haces", "como funcionas", "instrucciones"]
    ) or not texto_sin_robotina:
        return {
            "status": "success",
            "tipo_accion": "AYUDA",
            "reply": (
                "🤖 *¡Hola! Soy Robotina, asistente clínica de guardia (24/7)*\n\n"
                "Estoy conectada a la bitácora médica para registrar reportes y pendientes del turno.\n\n"
                "📋 *Consultas:* \n"
                "• `Robotina pendientes` ➔ Checklist nocturno (STAT, urgentes y controles).\n"
                "• `Robotina camas` ➔ Lista de camas y pacientes censados.\n"
                "• `Robotina como esta 6A` ➔ Ficha clínica y pendientes de una cama.\n\n"
                "✍ *Registro de resultados y pendientes:*\n"
                "• *Reportar resultado:* `Robotina la gasometria de 6a es pf 6.9`\n"
                "• *Anotar nuevo pendiente:* `6A: Gasometria STAT 21:30`\n"
                "• *Formato rápido:* `6A resultado: Lactato 1.4 normal`"
            ),
        }

    # Listado de camas activas
    if cmd in ("!camas", "camas") or any(w in cmd for w in ["que camas hay", "lista de camas", "camas censadas", "pacientes"]):
        camas_str = ", ".join(f"*{p.cama}* ({p.nombre_completo.split()[0]})" for p in bitacora.pacientes)
        return {
            "status": "success",
            "tipo_accion": "LISTA_CAMAS",
            "reply": f"🏥 *Camas activas censadas ({len(bitacora.pacientes)}):*\n{camas_str}\n\n_Para ver una cama escribe:_ `Robotina como esta 6A`",
        }

    # Parseo local de respaldo
    parsed = parse_whatsapp_incoming_message(texto_limpio)
    if not parsed:
        # Búsqueda local por apellido de paciente o exámenes (ej: "Que examenes de Rodríguez", "como esta rodriguez")
        cmd_norm = normalize_text(cmd)
        for p in bitacora.pacientes:
            for n in normalize_text(p.nombre_completo).split():
                if len(n) > 3 and re.search(r"\b" + re.escape(n) + r"\b", cmd_norm):
                    return {
                        "status": "success",
                        "tipo_accion": "CONSULTA_CAMA",
                        "reply": format_paciente_card(p),
                    }
        return {"status": "ignored", "reason": "No clinical pattern matched", "text": texto_limpio}

    if parsed["accion"] == "SALUDO_ROBOTINA":
        return {
            "status": "success",
            "tipo_accion": "SALUDO",
            "reply": "🤖 *¡A la orden! Aquí Robotina.* ¿En qué cama o pendiente necesitas asistencia? (Escribe `Robotina ayuda` para ver opciones).",
        }

    cama = parsed["cama"]
    paciente = next((p for p in bitacora.pacientes if p.cama.upper() == cama.upper()), None)
    if not paciente:
        camas_disponibles = ", ".join(p.cama for p in bitacora.pacientes[:8]) + "..."
        return {
            "status": "error",
            "reply": f"⚠ No se encontró la cama '{cama}' en la bitácora activa.\n_Camas disponibles:_ {camas_disponibles}",
        }

    # Consulta clínica de cama individual
    if parsed["accion"] == "CONSULTAR_CAMA":
        return {
            "status": "success",
            "tipo_accion": "CONSULTA_CAMA",
            "reply": format_paciente_card(paciente),
        }

    # Registro de resultado clínico
    if parsed["accion"] == "REGISTRAR_RESULTADO":
        resultado_valor = parsed.get("valor") or parsed["resultado"]
        estudio_kw = (parsed.get("estudio") or "").lower()
        modificado = False
        pend_modificado_desc = ""
        pends_nuevos = []

        for pend in paciente.pendientes:
            coincide_estudio = bool(estudio_kw and (estudio_kw in pend.descripcion.lower() or (estudio_kw == "gasometria" and "gas" in pend.descripcion.lower())))
            if pend.es_visita_nocturna and pend.estado == "PENDIENTE" and (coincide_estudio or not modificado):
                pends_nuevos.append(
                    pend.model_copy(
                        update={"estado": "COMPLETADO", "resultado_obtenido": resultado_valor}
                    )
                )
                modificado = True
                pend_modificado_desc = pend.descripcion
            else:
                pends_nuevos.append(pend)

        if not modificado:
            nuevo_desc = f"{parsed.get('estudio').capitalize() if parsed.get('estudio') else 'Reporte'} (WhatsApp)"
            nuevo_p = Pendiente(
                id=f"PEND-{cama}-WA",
                tipo=PendienteTipo.LABORATORIO,
                descripcion=nuevo_desc,
                prioridad=PendientePrioridad.STAT,
                turno=TurnoObjetivo.NOCTURNA_20_24,
                es_visita_nocturna=True,
                estado="COMPLETADO",
                resultado_obtenido=resultado_valor,
            )
            pends_nuevos.append(nuevo_p)
            pend_modificado_desc = nuevo_desc

        p_nuevo = paciente.model_copy(update={"pendientes": pends_nuevos})
        pacs_actualizados = [
            p_nuevo if p.cama.upper() == cama.upper() else p for p in bitacora.pacientes
        ]
        nueva_bit = bitacora.model_copy(update={"pacientes": pacs_actualizados})
        save_bitacora(nueva_bit, bitacora_path)

        return {
            "status": "success",
            "tipo_accion": "RESULTADO_REGISTRADO",
            "reply": (
                f"✔ *Resultado registrado por Robotina:*\n"
                f"🏥 *Cama {cama}:* {paciente.nombre_completo.split()[0]} {paciente.nombre_completo.split()[-1] if len(paciente.nombre_completo.split()) > 1 else ''}\n"
                f"📋 *Pendiente:* {pend_modificado_desc}\n"
                f"📊 *Resultado:* `{resultado_valor}`\n"
                f"✅ *Estado:* COMPLETADO"
            ),
        }

    # Creación de nuevo pendiente
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
