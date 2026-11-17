import asyncio
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from bitacora.bed_store import (
    actualizar_evolucion_cama,
    completar_pendiente_cama,
    consolidar_bitacora,
    crear_pendiente_cama,
    egresar_paciente_cama,
    get_cama,
    ingresar_paciente_cama,
    list_camas_activas,
    registrar_resultado_cama,
)
from bitacora.drive_evolutions_sync import (
    escribir_nota_robotina_en_drive_doc,
    sincronizar_evoluciones_drive,
)
from bitacora.excel_exporter import exportar_bitacora_xlsx, subir_xlsx_a_drive
from bitacora.groq_agent import call_groq_clinical_parser, set_groq_api_key, test_groq_connection
from bitacora.models import Paciente, Pendiente, PendientePrioridad, PendienteTipo, TurnoObjetivo
from bitacora.pdf_generator import generar_bitacora_completa
from bitacora.reminders import (
    crear_recordatorio,
    emitir_alerta_whatsapp,
    enviar_archivo_whatsapp,
    enviar_mensaje_whatsapp,
    enviar_pdf_whatsapp,
    formatear_recordatorios,
    mark_daily_14h_sync_done,
    mark_daily_pdf_published,
    mark_scheduled_sync_done,
    now_ecuador,
    parse_hora_recordatorio,
    should_publish_daily_pdf,
    should_run_daily_14h_sync,
    should_run_scheduled_sync,
    verificar_recordatorios_a_disparar,
)
from bitacora.schedule_loader import obtener_responsables_guardia
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


_NAME_STOPWORDS = {
    "paciente", "cama", "como", "esta", "estan", "examen", "examenes", "laboratorio",
    "laboratorios", "resultado", "resultados", "tiene", "sobre", "para", "desde",
    "drive", "bitacora", "agregar", "agregando", "ingresar", "ingresa", "anadir",
    "robotina", "ayuda", "ayudame", "encuentra", "guardia", "reporte", "favor",
    "ultimo", "ultimos", "ultima", "ultimas", "periferico", "perifericos",
    "hospitalizacion", "emergencia", "abreviados", "observacion", "cual", "cuales",
}


def find_patient_by_name(query: str, pacientes: list[Paciente]) -> Paciente | None:
    """Busca un paciente dando mayor peso a apellidos; si la consulta tiene 2+ palabras exige al menos 2 coincidencias."""
    q_norm = normalize_text(query)
    tokens = set(t for t in re.findall(r"[a-z0-9]+", q_norm) if len(t) > 3 and t not in _NAME_STOPWORDS)
    if not tokens:
        return None

    min_matches = 2 if len(tokens) >= 2 else 1
    best_pac: Paciente | None = None
    best_score = 0

    for p in pacientes:
        p_words = re.findall(r"[a-z0-9]+", normalize_text(p.nombre_completo))
        if not p_words:
            continue
        # En formato estándar (APELLIDO1 APELLIDO2 NOMBRE1 NOMBRE2), los primeros 2 tokens son apellidos
        surnames = set(p_words[:2]) if len(p_words) >= 3 else {p_words[0]}
        matched_words = [w for w in set(p_words) if w in tokens]
        if len(matched_words) < min_matches:
            continue
        score = sum(2 if w in surnames else 1 for w in matched_words)
        if score >= 2 and score > best_score:
            best_score = score
            best_pac = p
    return best_pac


def find_partial_candidates(query: str, pacientes: list[Paciente]) -> list[Paciente]:
    """Retorna pacientes con coincidencia parcial de apellido para pedir aclaración sin actuar por error."""
    q_norm = normalize_text(query)
    tokens = set(t for t in re.findall(r"[a-z0-9]+", q_norm) if len(t) > 3 and t not in _NAME_STOPWORDS)
    if not tokens:
        return []
    matches: list[Paciente] = []
    for p in pacientes:
        p_words = set(re.findall(r"[a-z0-9]+", normalize_text(p.nombre_completo)))
        if p_words & tokens:
            matches.append(p)
    return matches[:5]

def _disparar_nota_robotina_doc(paciente: Paciente, categoria: str, detalle: str) -> None:
    """Programa en segundo plano la escritura de una Nota de Robotina en el Google Doc del paciente."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(escribir_nota_robotina_en_drive_doc, paciente, categoria, detalle))
    except RuntimeError:
        pass


app = FastAPI(
    title="WhatsApp Webhook - Bitácora Médica & Robotina",
    description="Bridge HTTP para sincronizar mensajes de grupos de WhatsApp con la bitácora médica.",
)


def publicar_bitacora_diaria_pdf(chat_id: str | None = None) -> dict[str, Any]:
    """Consolida la bitácora, genera PDF + XLSX, los sube a Drive (si está configurado) y envía al grupo."""
    bitacora_path = get_active_bitacora_path()
    archivos = generar_bitacora_completa(bitacora_path)
    xlsx_path = exportar_bitacora_xlsx(bitacora_path)
    drive_res = subir_xlsx_a_drive(xlsx_path)
    bit_consolidada = consolidar_bitacora(bitacora_path)
    ahora_ec = now_ecuador()
    pdf_path = archivos.get("pdf", Path("reports/bitacora_guardia.pdf"))

    drive_line = (
        f"\n☁ *Excel en Google Drive:* {drive_res['webViewLink']}"
        if drive_res.get("ok") and drive_res.get("webViewLink")
        else ""
    )

    caption = (
        f"🌅 *ENTREGA DE GUARDIA AUTOMÁTICA ({ahora_ec.strftime('%d/%m/%Y %H:%M')} Ecuador)*\n\n"
        f"📄 *Bitácora Médica Oficial Consolidada (Quarto PDF, Web & Excel)*\n"
        f"🏥 *Periodo:* {bit_consolidada.periodo}\n"
        f"👥 *Pacientes Activos:* {len(bit_consolidada.pacientes)} camas censadas\n\n"
        "📥 *Descargar PDF:*\n"
        "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.pdf\n\n"
        "📊 *Descargar Excel (.xlsx):*\n"
        "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.xlsx\n\n"
        "🌐 *Ver reporte interactivo:*\n"
        "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.html"
        f"{drive_line}"
    )
    res_envio = enviar_pdf_whatsapp(pdf_path=pdf_path, caption=caption, chat_id=chat_id)
    if res_envio.get("ok"):
        mark_daily_pdf_published(ahora_ec)
        logger.info("PDF diario de las 05:00 AM publicado exitosamente en WhatsApp.")
    else:
        logger.warning(f"No se pudo publicar el PDF diario aún: {res_envio}")
    return {
        "ok": bool(res_envio.get("ok")),
        "envio": res_envio,
        "drive": drive_res,
        "hora_ecuador": ahora_ec.isoformat(),
    }


def ejecutar_sync_evoluciones_programada(slot: str = "14:00") -> dict[str, Any]:
    """Sincroniza las evoluciones de Drive (Gaibor, Torres, Mosquera), regenera PDF/Excel y publica archivos al grupo."""
    ahora_ec = now_ecuador()
    res = sincronizar_evoluciones_drive()
    if res.get("ok"):
        camas = ", ".join(res.get("updated_beds") or []) or "Ninguna coincidencia"
        guardia = obtener_responsables_guardia(ahora_ec, refresh_drive=False)
        resp_list = guardia.get("medicos_guardia") or []
        resp_txt = "\n".join(f"  • {r}" for r in resp_list) if resp_list else "  • Equipo de Guardia"

        caption_xlsx = (
            f"🔄 *BITÁCORA SINCRONIZADA ({ahora_ec.strftime('%d/%m/%Y %H:%M')} Ecuador - {slot})*\n\n"
            f"👨‍⚕️ *Responsables de guardia:*\n{resp_txt}\n\n"
            f"📂 *Docs de evolución leídos:* {res.get('docs_leidos', 0)}\n"
            f"🏥 *Camas actualizadas:* {camas}\n\n"
            "📥 *Descarga directa Excel:*\n"
            "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.xlsx\n"
            "📥 *Descarga directa PDF:*\n"
            "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.pdf"
        )
        xlsx_path = Path("reports/bitacora_guardia.xlsx")
        pdf_path = Path("reports/bitacora_guardia.pdf")
        if xlsx_path.exists():
            enviar_archivo_whatsapp(
                file_path=xlsx_path,
                caption=caption_xlsx,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        else:
            enviar_mensaje_whatsapp(caption_xlsx)
        if pdf_path.exists():
            enviar_pdf_whatsapp(
                pdf_path=pdf_path,
                caption=f"📄 *Bitácora PDF sincronizada ({ahora_ec.strftime('%d/%m/%Y')} - {slot})*",
            )
        mark_scheduled_sync_done(slot, ahora_ec)
    elif res.get("needs_credentials"):
        logger.warning(f"Sync {slot} omitido: falta GOOGLE_SERVICE_ACCOUNT_JSON en Railway.")
        mark_scheduled_sync_done(slot, ahora_ec)
    return res


def ejecutar_sync_evoluciones_14h() -> dict[str, Any]:
    return ejecutar_sync_evoluciones_programada("14:00")


@app.on_event("startup")
async def startup_event():
    """Inicia el worker para alarmas clínicas, PDF 05:00 AM y sync de evoluciones 14:00 y 22:00 Ecuador."""
    async def reminder_loop():
        while True:
            try:
                alertas = verificar_recordatorios_a_disparar()
                for al in alertas:
                    emitir_alerta_whatsapp(al)

                if should_publish_daily_pdf():
                    logger.info("Hora programada (05:00 AM Ecuador): generando y publicando PDF de bitácora...")
                    await asyncio.to_thread(publicar_bitacora_diaria_pdf)

                sync_slot = should_run_scheduled_sync()
                if sync_slot:
                    logger.info(f"Hora programada ({sync_slot} Ecuador): sincronizando evoluciones de Drive...")
                    await asyncio.to_thread(ejecutar_sync_evoluciones_programada, sync_slot)
            except Exception as e:
                logger.error(f"Error en worker de recordatorios, PDF y sync 14:00/22:00: {e}")
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

    if pac.observaciones:
        lines.append("📝 *Evolución / Notas:*")
        for obs in pac.observaciones[-5:]:
            lines.append(f"• {obs}")
        lines.append("")

    if pac.laboratorios:
        lines.append("🔬 *Últimos Exámenes / Laboratorios:*")
        for lab in pac.laboratorios[-4:]:
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


@app.get("/descargar/bitacora.xlsx")
def download_xlsx() -> FileResponse:
    """Descarga directa de la bitácora médica consolidada en formato Excel (.xlsx)."""
    bitacora_path = get_active_bitacora_path()
    xlsx_path = exportar_bitacora_xlsx(bitacora_path)
    if not xlsx_path.exists():
        raise HTTPException(status_code=404, detail="No se pudo generar el archivo Excel.")
    return FileResponse(
        str(xlsx_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="bitacora_guardia.xlsx",
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


@app.post("/api/publicar-pdf-ahora")
def publicar_pdf_ahora(chat_id: str | None = None) -> dict[str, Any]:
    """Genera el PDF de la bitácora y lo publica inmediatamente en el grupo de WhatsApp."""
    return publicar_bitacora_diaria_pdf(chat_id=chat_id)


@app.api_route("/api/sincronizar-evoluciones", methods=["GET", "POST"])
def sincronizar_evoluciones_endpoint(folder_id: str | None = None) -> dict[str, Any]:
    """Sincroniza manualmente las evoluciones de Drive y regenera PDF y Excel."""
    try:
        return sincronizar_evoluciones_drive(folder_id=folder_id)
    except Exception as e:
        logger.exception("Error inesperado en /api/sincronizar-evoluciones")
        return {"ok": False, "error": str(e)}


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
    cmd_norm = normalize_text(cmd)

    # 0. MANUAL DE OPERACIONES DE ROBOTINA (respuesta directa e instantánea)
    if cmd_norm in ("manual", "!manual", "manual de uso", "manual de operaciones") or any(
        w in cmd_norm
        for w in [
            "manual de operaciones",
            "dame el manual",
            "dame tu manual",
            "ver el manual",
            "manual de robotina",
        ]
    ):
        return {
            "status": "success",
            "tipo_accion": "MANUAL_OPERACIONES",
            "reply": (
                "📖 *MANUAL DE OPERACIONES — ROBOTINA (24/7)*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "No necesitas comandos rígidos: mencióname (`Robotina ...`) en lenguaje natural.\n\n"
                "📋 *1. CONSULTAS CLÍNICAS*\n"
                "• `Robotina cómo está la 6A` ➔ Ficha clínica, labs y pendientes.\n"
                "• `Robotina qué exámenes tiene don Rodríguez` ➔ Busca por apellido.\n"
                "• `Robotina qué antibiótico tiene 6A?` ➔ Consulta puntual con IA.\n"
                "• `Robotina pendientes` ➔ Checklist nocturno de guardia.\n"
                "• `Robotina camas` ➔ Lista de todas las camas censadas.\n\n"
                "🔬 *2. RESULTADOS Y EVOLUCIÓN*\n"
                "• `Robotina la gasometría de 6A es pH 7.38, lactato 1.2`\n"
                "• `6A resultado: Sodio 136 normal`\n"
                "• `Robotina evolución 6A: paciente afebril, estable`\n\n"
                "📌 *3. PENDIENTES DE GUARDIA*\n"
                "• *Crear:* `6A: Gasometría STAT 21:30` o `Robotina anota en 8A control de sodio`\n"
                "• *Completar:* `Robotina ya se completó el sodio de 8A`\n\n"
                "🏥 *4. INGRESOS Y EGRESOS*\n"
                "• *Ingreso:* `Robotina ingresa en 6A a Juan Pérez de 54 años con dx Neumonía`\n"
                "• *Egreso:* `Robotina saca al paciente de 6A por alta médica`\n\n"
                "⏰ *5. RECORDATORIOS (HORA ECUADOR)*\n"
                "• *Programar:* `Robotina recuérdanos a las 7 pm el sodio de 8A`\n"
                "• *Consultar:* `Robotina recordatorios`\n\n"
                "📄 *6. BITÁCORA PDF Y EXCEL (ENTREGA DE GUARDIA)*\n"
                "• *Automático:* Todos los días a las *05:00 AM (Ecuador)* envío el PDF al grupo y exporto el Excel.\n"
                "• *Manual PDF:* `Robotina genera el pdf`\n"
                "• *Manual Excel / Drive:* `Robotina guarda la bitácora en xlsx en el drive`\n"
                "• 📥 PDF: https://bitacora-guardia-production.up.railway.app/descargar/bitacora.pdf\n"
                "• 📊 Excel: https://bitacora-guardia-production.up.railway.app/descargar/bitacora.xlsx\n"
                "• 🌐 Web: https://bitacora-guardia-production.up.railway.app/descargar/bitacora.html\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "💡 _Si el QR se desconecta, entra a:_ https://bitacora-guardia-production.up.railway.app"
            ),
        }

    bitacora_path = get_active_bitacora_path()
    bitacora = consolidar_bitacora(bitacora_path)

    # 0.5 EXPORTAR BITÁCORA A EXCEL (.XLSX) Y SUBIR A GOOGLE DRIVE
    if cmd_norm in ("excel", "xlsx", "!excel", "!xlsx") or any(
        w in cmd_norm
        for w in [
            "en xlsx",
            "en excel",
            "a excel",
            "a xlsx",
            "el excel",
            "el xlsx",
            "guarda en drive",
            "graba en drive",
            "sube al drive",
            "subir al drive",
            "guardar en drive",
            "grabar en drive",
        ]
    ):
        xlsx_path = exportar_bitacora_xlsx(bitacora_path)
        drive_res = subir_xlsx_a_drive(xlsx_path)
        if drive_res.get("ok"):
            drive_msg = (
                f"☁ *Guardado en Google Drive ({drive_res.get('action', 'subido')}):*\n"
                f"📁 Archivo: `{drive_res.get('name')}`\n"
                f"🔗 {drive_res.get('webViewLink')}"
            )
        elif drive_res.get("needs_credentials"):
            drive_msg = (
                "⚠ *Nota Google Drive:* El archivo `.xlsx` se generó correctamente, pero falta "
                "configurar `GOOGLE_SERVICE_ACCOUNT_JSON` en Railway para subirlo automáticamente a tu Drive."
            )
        else:
            drive_msg = f"⚠ *No se pudo subir a Google Drive:* {drive_res.get('error', 'Error desconocido')}"

        return {
            "status": "success",
            "tipo_accion": "EXPORTAR_XLSX_DRIVE",
            "reply": (
                "📊 *Bitácora Médica Exportada a Excel (.xlsx)*\n\n"
                f"🏥 *Periodo:* {bitacora.periodo}\n"
                f"👥 *Pacientes Activos:* {len(bitacora.pacientes)} camas consolidadas\n\n"
                f"{drive_msg}\n\n"
                "📥 *Descargar Excel directamente:*\n"
                "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.xlsx"
            ),
        }

    # 1. ORQUESTADOR CLÍNICO GROQ (Groq decide qué script Python ejecutar o responde directamente)
    camas_info = [
        {
            "cama": p.cama,
            "area": p.area,
            "nombre": p.nombre_completo,
            "edad": p.edad,
            "dx": "; ".join(p.diagnosticos_activos) if p.diagnosticos_activos else "N/D",
            "meds": ", ".join(f"{m.nombre} {m.dosis}" for m in p.medicaciones if not m.suspendido) or "N/D",
            "labs": ", ".join(f"{l.tipo_estudio} ({l.fecha_hora}): {l.valores}" for l in p.laboratorios[-3:]) or "N/D",
            "obs": " | ".join(p.observaciones[-3:]) or "N/D",
            "alertas": ", ".join(p.alertas_criticas) or "N/D",
            "pendientes": ", ".join(
                f"{pend.descripcion} [{pend.estado}]"
                + (f" ({pend.resultado_obtenido})" if pend.resultado_obtenido else "")
                for pend in p.pendientes
            ) or "Ninguno",
        }
        for p in bitacora.pacientes
    ]

    groq_res = call_groq_clinical_parser(texto_limpio, camas_info)
    if groq_res and isinstance(groq_res, dict) and "intencion" in groq_res:
        intencion = groq_res["intencion"]

        if intencion == "OTRO":
            return {"status": "ignored", "reason": "Groq classified as non-clinical chatter", "text": texto_limpio}

        if intencion == "RESPUESTA_DIRECTA" and groq_res.get("respuesta_directa"):
            return {
                "status": "success",
                "tipo_accion": "RESPUESTA_GROQ",
                "reply": str(groq_res["respuesta_directa"]),
            }

        if intencion == "EGRESAR_PACIENTE":
            cama_target = str(groq_res.get("cama") or "").strip().upper()
            pac_query = str(groq_res.get("paciente") or "").strip()

            # Extraer también cualquier nombre que el usuario haya puesto en el mensaje (ej: "paciente Vasconez Norma")
            m_pac_msg = re.search(r"(?:paciente|sr\.?|sra\.?|don|doña)\s+([a-záéíóúñA-ZÁÉÍÓÚÑ ]{4,40})(?:\s+se\s+va|\s+alta|\s+egres|\s+por\s+favor|\s+falleci|\.|\,|$)", texto_limpio, re.IGNORECASE)
            if m_pac_msg and not pac_query:
                pac_query = m_pac_msg.group(1).strip()

            # SI EL USUARIO ESPECIFICÓ UN PACIENTE (con o sin cama deducida por Groq):
            if pac_query:
                pac_match = find_patient_by_name(pac_query, bitacora.pacientes)
                if pac_match:
                    cama_target = pac_match.cama
                else:
                    # Si Groq asoció incorrectamente a otro paciente (ej: Carlos en vez de Norma)
                    cands = find_partial_candidates(pac_query, bitacora.pacientes)
                    if cands:
                        cands_txt = "\n".join(f"• *Cama {c.cama}:* {c.nombre_completo}" for c in cands)
                        return {
                            "status": "error",
                            "reply": (
                                f"⚠ No encontré coincidencia exacta para *{pac_query}* en la bitácora activa.\n\n"
                                f"Pacientes censados con apellido similar:\n{cands_txt}\n\n"
                                "Por seguridad no egresé ninguna cama. Por favor indícame la *cama exacta* (ej: `Robotina da de alta la cama ...`)."
                            ),
                        }
                    return {
                        "status": "error",
                        "reply": f"⚠ No encontré a *{pac_query}* en la bitácora activa. Por favor indícame la cama exacta si deseas liberarla.",
                    }

            if not cama_target:
                return {
                    "status": "error",
                    "reply": "⚠ Indica qué cama o paciente deseas egresar (ej: `Robotina saca al paciente de 6A`).",
                }
            motivo = str(groq_res.get("estudio") or groq_res.get("nota") or "Alta / Egreso")
            res_eg = egresar_paciente_cama(cama_target, motivo=motivo)
            if not res_eg["ok"]:
                return {"status": "success", "tipo_accion": "CAMA_LIBRE", "reply": f"🏥 {res_eg['reason']}"}
            return {
                "status": "success",
                "tipo_accion": "EGRESAR_PACIENTE",
                "reply": (
                    f"📤 *Paciente Egresado / Cama Liberada:*\n"
                    f"🏥 *Cama {res_eg['cama']}:* {res_eg['paciente']}\n"
                    f"📋 *Motivo:* {res_eg['motivo']}\n"
                    f"🗂 _Matriz de cama {res_eg['cama']} archivada y bitácora consolidada._"
                ),
            }

        if intencion == "INGRESAR_PACIENTE":
            cama_target = str(groq_res.get("cama") or "").strip().upper()
            nombre_pac = str(groq_res.get("paciente") or "").strip()
            if not cama_target or not nombre_pac:
                return {
                    "status": "error",
                    "reply": "⚠ Para ingresar un paciente necesito al menos la cama y el nombre (ej: `Robotina ingresa en 6A a Juan Pérez de 50 años con dx Neumonía`).",
                }
            edad_pac = str(groq_res.get("edad") or "N/D")
            area_pac = str(groq_res.get("area") or "HOSPITALIZACION").upper()
            if area_pac not in ("HOSPITALIZACION", "PERIFERICOS", "EMERGENCIA_OBSERVACION"):
                area_pac = "HOSPITALIZACION"
            dx_pac = groq_res.get("diagnostico")
            obs_pac = groq_res.get("nota")
            res_ing = ingresar_paciente_cama(
                cama_id=cama_target,
                nombre_completo=nombre_pac,
                edad=edad_pac,
                area=area_pac,  # type: ignore[arg-type]
                diagnostico=dx_pac,
                observacion=obs_pac,
            )
            pac_new = res_ing["paciente"]
            _disparar_nota_robotina_doc(pac_new, "INGRESO", f"Cama {pac_new.cama} | Dx: {dx_pac or 'Por definir'}")
            return {
                "status": "success",
                "tipo_accion": "INGRESAR_PACIENTE",
                "reply": (
                    f"📥 *Nuevo Ingreso Registrado:*\n"
                    f"🏥 *Cama {pac_new.cama}:* {pac_new.nombre_completo} ({pac_new.edad} años)\n"
                    f"📍 *Área:* {pac_new.area}\n"
                    f"🩺 *Dx:* {dx_pac or 'Por definir'}\n"
                    f"🗂 _Matriz individual creada para cama {pac_new.cama}._"
                ),
            }

        if intencion == "ACTUALIZAR_EVOLUCION":
            cama_target = str(groq_res.get("cama") or "").strip().upper()
            nota = str(groq_res.get("nota") or groq_res.get("resultado") or groq_res.get("estudio") or "").strip()
            dx_new = groq_res.get("diagnostico")
            if not cama_target or not nota:
                return {
                    "status": "error",
                    "reply": "⚠ Indica la cama y la nota de evolución (ej: `Robotina evolución de 6A: paciente estable afebril`).",
                }
            res_ev = actualizar_evolucion_cama(cama_target, nota=nota, diagnostico=dx_new)
            if not res_ev["ok"]:
                return {"status": "error", "reply": f"⚠ {res_ev['reason']}"}
            pac_ev = res_ev["paciente"]
            _disparar_nota_robotina_doc(pac_ev, "EVOLUCIÓN", res_ev["nota"])
            return {
                "status": "success",
                "tipo_accion": "ACTUALIZAR_EVOLUCION",
                "reply": (
                    f"📝 *Evolución Registrada en Cama {pac_ev.cama}:*\n"
                    f"👤 *Paciente:* {pac_ev.nombre_completo}\n"
                    f"📌 *Nota:* {res_ev['nota']}"
                ),
            }

        if intencion == "CONSULTAR_RESUMEN":
            return {
                "status": "success",
                "tipo_accion": "CONSULTA_RESUMEN",
                "reply": format_whatsapp_night_summary(bitacora),
            }

        if intencion == "CONSULTAR_CAMAS":
            if not bitacora.pacientes:
                return {
                    "status": "success",
                    "tipo_accion": "LISTA_CAMAS",
                    "reply": "🏥 *No hay camas ocupadas actualmente en la bitácora.*",
                }
            camas_str = ", ".join(f"*{p.cama}* ({p.nombre_completo.split()[0]})" for p in bitacora.pacientes)
            return {
                "status": "success",
                "tipo_accion": "LISTA_CAMAS",
                "reply": f"🏥 *Camas activas censadas ({len(bitacora.pacientes)}):*\n{camas_str}\n\n_Para ver una cama escribe:_ `Robotina como esta 6A`",
            }

        if intencion == "GENERAR_PDF":
            generar_bitacora_completa(bitacora_path)
            bit_consolidada = consolidar_bitacora(bitacora_path)
            return {
                "status": "success",
                "tipo_accion": "GENERAR_PDF",
                "reply": (
                    "📄 *Bitácora Médica Oficial Consolidada y Generada con Quarto (PDF & Web)*\n\n"
                    f"🏥 *Periodo:* {bit_consolidada.periodo}\n"
                    f"👥 *Total Pacientes Activos:* {len(bit_consolidada.pacientes)} camas consolidadas\n"
                    f"🌙 *Checklist nocturno:* Actualizado con las novedades de cada matriz de cama.\n\n"
                    "📥 *Descargar PDF para Entrega de Guardia:*\n"
                    "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.pdf\n\n"
                    "🌐 *Ver reporte interactivo en línea:*\n"
                    "👉 https://bitacora-guardia-production.up.railway.app/descargar/bitacora.html"
                ),
            }

        if intencion == "CREAR_PENDIENTE" and groq_res.get("cama"):
            cama_target = str(groq_res["cama"]).strip().upper()
            desc = str(groq_res.get("estudio") or groq_res.get("nota") or "Control clínico").strip()
            prio = str(groq_res.get("prioridad") or "URGENTE").upper()
            horario = groq_res.get("horario")
            res_p = crear_pendiente_cama(cama_target, desc, prioridad=prio, horario=horario)
            if not res_p["ok"]:
                return {"status": "error", "reply": f"⚠ {res_p['reason']}"}
            pac_p = res_p["paciente"]
            _disparar_nota_robotina_doc(pac_p, "PENDIENTE", f"[{prio}] {desc}")
            return {
                "status": "success",
                "tipo_accion": "PENDIENTE_CREADO",
                "reply": f"📌 *Cama {pac_p.cama}* ({pac_p.nombre_completo.split()[0]}): Pendiente registrado ➔ [{prio}] {desc}.",
            }

        if intencion == "COMPLETAR_PENDIENTE" and groq_res.get("cama"):
            cama_target = str(groq_res["cama"]).strip().upper()
            estudio = str(groq_res.get("estudio") or "").strip()
            resultado = groq_res.get("resultado")
            res_c = completar_pendiente_cama(cama_target, estudio=estudio, resultado=resultado)
            if not res_c["ok"]:
                return {"status": "error", "reply": f"⚠ {res_c['reason']}"}
            pac_c = res_c["paciente"]
            _disparar_nota_robotina_doc(
                pac_c,
                "PENDIENTE COMPLETADO",
                f"{res_c['descripcion']}" + (f" -> {resultado}" if resultado else ""),
            )
            return {
                "status": "success",
                "tipo_accion": "PENDIENTE_COMPLETADO",
                "reply": f"✅ *Pendiente completado en Cama {pac_c.cama}:* {res_c['descripcion']}",
            }

        if intencion == "CREAR_RECORDATORIO":
            cama_target = str(groq_res.get("cama") or "").upper()
            pac = get_cama(cama_target) if cama_target else None
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
                    "🤖 *¡Hola! Soy Robotina, orquestadora clínica con Groq IA (24/7)*\n\n"
                    "Puedes hablarme con total lenguaje natural sin comandos rígidos:\n\n"
                    "• *Consultar paciente o exámenes:* `Robotina cómo sigue don Rodriguez?` o `qué antibiótico tiene 6A?`\n"
                    "• *Ingresar / Sacar paciente:* `Robotina saca al paciente de 6A` o `ingresa en 6A a Juan Pérez 50a dx Neumonía`\n"
                    "• *Evolución / Resultados:* `Robotina evolución 6A: estable` o `la gaso de 6a dio ph 6.9`\n"
                    "• *Recordatorios y PDF Quarto:* `Robotina recuérdanos a las 7 el sodio de 8A` o `genera el pdf`"
                ),
            }

        if intencion == "CONSULTAR_CAMA":
            cama_target = str(groq_res.get("cama") or "").strip().upper()
            pac = get_cama(cama_target) if cama_target else None
            if not pac and groq_res.get("paciente"):
                pac = find_patient_by_name(str(groq_res["paciente"]), bitacora.pacientes)
            if pac:
                return {
                    "status": "success",
                    "tipo_accion": "CONSULTA_CAMA",
                    "reply": format_paciente_card(pac),
                }
            if cama_target:
                return {
                    "status": "success",
                    "tipo_accion": "CAMA_LIBRE",
                    "reply": f"🏥 *Cama {cama_target}:* Actualmente *LIBRE* (sin paciente asignado).",
                }
            if groq_res.get("paciente"):
                return {
                    "status": "success",
                    "tipo_accion": "PACIENTE_NO_ENCONTRADO",
                    "reply": f"⚠ No encontré al paciente *{groq_res['paciente']}* en el censo activo.",
                }

        if intencion == "REGISTRAR_RESULTADO" and groq_res.get("cama"):
            cama_target = str(groq_res["cama"]).strip().upper()
            estudio = groq_res.get("estudio") or "Reporte"
            val = groq_res.get("resultado") or ""
            alerta = groq_res.get("comentario_clinico")
            res_reg = registrar_resultado_cama(cama_target, estudio, val, alerta=alerta)
            if not res_reg["ok"]:
                return {"status": "error", "reply": f"⚠ {res_reg['reason']}"}
            pac = res_reg["paciente"]
            _disparar_nota_robotina_doc(
                pac,
                "RESULTADO",
                f"{res_reg['pend_desc']}: {val}" + (f" (Nota: {alerta})" if alerta else ""),
            )
            reply_msg = (
                f"✔ *Resultado registrado por Robotina (Groq IA):*\n"
                f"🏥 *Cama {pac.cama}:* {pac.nombre_completo.split()[0]} {pac.nombre_completo.split()[-1] if len(pac.nombre_completo.split()) > 1 else ''}\n"
                f"📋 *Estudio:* {res_reg['pend_desc']}\n"
                f"📊 *Resultado:* `{val}`\n"
                f"✅ *Estado:* COMPLETADO"
            )
            if alerta:
                reply_msg += f"\n⚠ *Nota clínica:* {alerta}"
            return {"status": "success", "tipo_accion": "RESULTADO_REGISTRADO", "reply": reply_msg}

    # 2. MOTOR DE RESPALDO (SOLO SI GROQ ESTÁ OFFLINE O SIN API KEY)
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
        # Si piden agregar/ingresar un paciente sin especificar cama/datos (ej. "agregando ... que se encuentra en el drive")
        if any(v in cmd_norm for v in ("agreg", "ingres", "anad", "nuevo paciente")):
            return {
                "status": "error",
                "reply": (
                    "⚠ Para ingresar un paciente nuevo necesito la *cama*, el *nombre completo*, "
                    "la *edad* y el *diagnóstico* (no puedo buscar pacientes nuevos en archivos de Drive desde WhatsApp).\n\n"
                    "Escríbeme así:\n"
                    "`Robotina ingresa en <CAMA> a <NOMBRE> de <EDAD> años con dx <DIAGNÓSTICO>`"
                ),
            }
        # Búsqueda local estricta por apellido de paciente (ej: "Que examenes de Rodríguez", "como esta rodriguez")
        pac_local = find_patient_by_name(cmd, bitacora.pacientes)
        if pac_local:
            return {
                "status": "success",
                "tipo_accion": "CONSULTA_CAMA",
                "reply": format_paciente_card(pac_local),
            }
        return {"status": "ignored", "reason": "No clinical pattern matched", "text": texto_limpio}

    if parsed["accion"] == "SALUDO_ROBOTINA":
        return {
            "status": "success",
            "tipo_accion": "SALUDO",
            "reply": "🤖 *¡A la orden! Aquí Robotina.* ¿En qué cama o pendiente necesitas asistencia? (Escribe `Robotina ayuda` para ver opciones).",
        }

    cama = parsed["cama"]
    paciente = get_cama(cama)
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
        estudio_kw = parsed.get("estudio") or "Reporte"
        res_reg = registrar_resultado_cama(cama, estudio_kw, resultado_valor)
        if not res_reg["ok"]:
            return {"status": "error", "reply": f"⚠ {res_reg['reason']}"}
        _disparar_nota_robotina_doc(
            res_reg["paciente"],
            "RESULTADO",
            f"{res_reg['pend_desc']}: {resultado_valor}",
        )
        return {
            "status": "success",
            "tipo_accion": "RESULTADO_REGISTRADO",
            "reply": (
                f"✔ *Resultado registrado por Robotina:*\n"
                f"🏥 *Cama {cama}:* {paciente.nombre_completo.split()[0]} {paciente.nombre_completo.split()[-1] if len(paciente.nombre_completo.split()) > 1 else ''}\n"
                f"📋 *Pendiente:* {res_reg['pend_desc']}\n"
                f"📊 *Resultado:* `{resultado_valor}`\n"
                f"✅ *Estado:* COMPLETADO"
            ),
        }

    # Creación de nuevo pendiente
    elif parsed["accion"] == "CREAR_PENDIENTE":
        res_p = crear_pendiente_cama(
            cama,
            parsed["descripcion"],
            prioridad=parsed["prioridad"].value,
            horario=parsed["horario"],
        )
        if not res_p["ok"]:
            return {"status": "error", "reply": f"⚠ {res_p['reason']}"}
        _disparar_nota_robotina_doc(
            res_p["paciente"],
            "PENDIENTE",
            f"[{parsed['prioridad'].value}] {parsed['descripcion']}",
        )
        return {
            "status": "success",
            "tipo_accion": "PENDIENTE_CREADO",
            "reply": f"📌 *Cama {cama}* ({paciente.nombre_completo.split()[0]}): Pendiente obligatorio registrado para la visita nocturna ➔ [{parsed['prioridad'].value}] {parsed['descripcion']}.",
        }

    return {"status": "unhandled"}


@app.get("/api/camas")
def get_camas_matrix() -> dict[str, Any]:
    """Lista todas las matrices individuales de camas activas."""
    camas = list_camas_activas()
    return {
        "total_ocupadas": len(camas),
        "camas": [p.model_dump(mode="json") for p in camas],
    }
