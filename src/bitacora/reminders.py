"""Módulo de Recordatorios y Alarmas Clínicas para Robotina."""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)
REMINDERS_FILE = Path("data/recordatorios.json")
AUTO_PDF_STATE_FILE = Path("data/auto_pdf_state.json")
BRIDGE_URL = os.getenv("BRIDGE_URL", "http://127.0.0.1:8080")
ECUADOR_TZ = ZoneInfo("America/Guayaquil")
AUTO_PDF_HOUR = os.getenv("AUTO_PDF_HOUR", "05:00")


def now_ecuador() -> datetime:
    """Retorna la fecha y hora actual en Ecuador (America/Guayaquil, UTC-5)."""
    return datetime.now(ECUADOR_TZ)


def get_reminders_path() -> Path:
    REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not REMINDERS_FILE.exists():
        REMINDERS_FILE.write_text("[]", encoding="utf-8")
    return REMINDERS_FILE


def cargar_recordatorios() -> list[dict[str, Any]]:
    path = get_reminders_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error cargando recordatorios: {e}")
        return []


def guardar_recordatorios(recs: list[dict[str, Any]]) -> None:
    path = get_reminders_path()
    path.write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_hora_recordatorio(raw_text: str) -> str:
    """Extrae y normaliza horarios como 'a las 7', '7 am', '07:00', '19:30' a formato HH:MM."""
    raw = raw_text.strip().lower()
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw)
    if not m:
        return "07:00"
    h = int(m.group(1))
    mins = int(m.group(2) or 0)
    ampm = m.group(3)

    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif not ampm and h <= 7 and "noche" not in raw and "pm" not in raw:
        # Por contexto de guardia hospitalaria, si piden 'a las 7', 'a las 6', o 'a las 5' suele ser AM
        pass

    return f"{h:02d}:{mins:02d}"


def crear_recordatorio(
    hora_str: str,
    cama: str,
    paciente_nombre: str,
    tarea: str,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """Registra un nuevo recordatorio clínico."""
    recs = cargar_recordatorios()
    nuevo_id = f"REC-{len(recs) + 1:03d}"
    hora_norm = parse_hora_recordatorio(hora_str)

    nuevo = {
        "id": nuevo_id,
        "hora_programada": hora_norm,
        "cama": cama,
        "paciente": paciente_nombre,
        "tarea": tarea,
        "chat_id": chat_id,
        "estado": "PENDIENTE",
        "creado_en": now_ecuador().strftime("%Y-%m-%d %H:%M:%S"),
    }
    recs.append(nuevo)
    guardar_recordatorios(recs)
    return nuevo


def listar_recordatorios_activos() -> list[dict[str, Any]]:
    recs = cargar_recordatorios()
    return [r for r in recs if r.get("estado") == "PENDIENTE"]


def formatear_recordatorios() -> str:
    activos = listar_recordatorios_activos()
    if not activos:
        return "⏰ *Robotina:* No hay recordatorios ni alarmas pendientes para esta guardia."

    lines = [
        "⏰ *RECORDATORIOS Y ALARMAS ACTIVAS DE GUARDIA*",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for r in activos:
        lines.append(
            f"• 🕒 *{r['hora_programada']}* ➔ Cama *{r['cama']}* ({r['paciente'].split()[0]}): {r['tarea']}"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 _Robotina emitirá una alerta automática en el grupo al llegar cada hora._")
    return "\n".join(lines)


def verificar_recordatorios_a_disparar(ahora: datetime | None = None) -> list[dict[str, Any]]:
    """Verifica si ha llegado la hora de algún recordatorio pendiente (hora de Ecuador)."""
    ahora_ec = ahora or now_ecuador()
    hora_actual = ahora_ec.strftime("%H:%M")
    recs = cargar_recordatorios()
    disparar = []
    modificado = False

    for r in recs:
        if r.get("estado") == "PENDIENTE" and r.get("hora_programada") == hora_actual:
            r["estado"] = "DISPARADO"
            r["disparado_en"] = ahora_ec.strftime("%Y-%m-%d %H:%M:%S")
            disparar.append(r)
            modificado = True

    if modificado:
        guardar_recordatorios(recs)

    return disparar


def emitir_alerta_whatsapp(recordatorio: dict[str, Any]) -> bool:
    """Envía la alerta del recordatorio al grupo de WhatsApp mediante el Bridge."""
    chat_id = recordatorio.get("chat_id")
    mensaje = (
        f"⏰🔔 *¡RECORDATORIO DE GUARDIA ({recordatorio['hora_programada']})!* 🔔⏰\n\n"
        f"🏥 *Cama {recordatorio['cama']}:* {recordatorio['paciente']}\n"
        f"📋 *Acción pendiente:* {recordatorio['tarea']}\n\n"
        f"👨‍⚕️ _Por favor ejecutar y reportar resultado a Robotina._"
    )

    try:
        data = json.dumps({"chatId": chat_id, "preferGroup": True, "text": mensaje}).encode("utf-8")
        req = urllib.request.Request(
            f"{BRIDGE_URL}/api/send-message",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"Error emitiendo alerta de recordatorio a WhatsApp: {e}")
        return False


def should_publish_daily_pdf(ahora: datetime | None = None, target_hhmm: str | None = None) -> bool:
    """Determina si corresponde publicar automáticamente el PDF diario (05:00 AM Ecuador)."""
    ahora_ec = ahora or now_ecuador()
    objetivo = (target_hhmm or AUTO_PDF_HOUR).strip()
    try:
        target_h, target_m = [int(x) for x in objetivo.split(":", 1)]
    except Exception:
        target_h, target_m = 5, 0

    # Ventana de 05:00 a 05:59 hora Ecuador para tolerar reinicios puntuales cerca de las 5 AM
    if ahora_ec.hour != target_h or ahora_ec.minute < target_m:
        return False

    today_str = ahora_ec.strftime("%Y-%m-%d")
    if AUTO_PDF_STATE_FILE.exists():
        try:
            state = json.loads(AUTO_PDF_STATE_FILE.read_text(encoding="utf-8"))
            if state.get("last_published_date") == today_str:
                return False
        except Exception as e:
            logger.warning(f"Error leyendo {AUTO_PDF_STATE_FILE}: {e}")

    return True


def mark_daily_pdf_published(ahora: datetime | None = None) -> None:
    """Registra que el PDF diario ya fue publicado hoy en hora de Ecuador."""
    ahora_ec = ahora or now_ecuador()
    AUTO_PDF_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_published_date": ahora_ec.strftime("%Y-%m-%d"),
        "published_at": ahora_ec.isoformat(),
    }
    AUTO_PDF_STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def enviar_pdf_whatsapp(
    pdf_path: Path,
    caption: str,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """Envía el archivo PDF compilado al grupo de WhatsApp mediante el Bridge."""
    payload: dict[str, Any] = {
        "chatId": chat_id,
        "preferGroup": True,
        "text": caption,
    }
    if pdf_path.exists():
        payload["documentPath"] = str(pdf_path.resolve())
        payload["fileName"] = pdf_path.name
        payload["mimetype"] = "application/pdf"
        payload["caption"] = caption

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{BRIDGE_URL}/api/send-message",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return {"ok": resp.status == 200, "status": resp.status, "response": body}
    except Exception as e:
        logger.error(f"Error enviando PDF diario a WhatsApp: {e}")
        return {"ok": False, "error": str(e)}
