"""Módulo de Inteligencia Clínica con Groq (Gemma / Llama) para Robotina."""

import json
import logging
import os
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
KEY_FILE = Path("data/groq_key.txt")

MODELS_TO_TRY = [
    os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]


def get_groq_api_key(api_key: str | None = None) -> str | None:
    """Obtiene la clave de Groq desde parámetro, variable de entorno o archivo data/groq_key.txt."""
    if api_key and api_key.strip("'\" \t\r\n"):
        return api_key.strip("'\" \t\r\n")

    env_key = os.getenv("GROQ_API_KEY")
    if env_key and env_key.strip("'\" \t\r\n"):
        return env_key.strip("'\" \t\r\n")

    if KEY_FILE.exists():
        try:
            k = KEY_FILE.read_text(encoding="utf-8").strip("'\" \t\r\n")
            if k:
                return k
        except Exception:
            pass

    return None


def set_groq_api_key(key: str) -> None:
    """Guarda la clave de Groq de forma persistente en data/groq_key.txt y en memoria."""
    cleaned = key.strip("'\" \t\r\n")
    os.environ["GROQ_API_KEY"] = cleaned
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(cleaned, encoding="utf-8")


def test_groq_connection(api_key: str | None = None) -> dict[str, Any]:
    """Prueba la conexión a la API de Groq y devuelve el diagnóstico detallado."""
    key = get_groq_api_key(api_key)
    if not key:
        return {
            "status": "error",
            "message": "GROQ_API_KEY no encontrada ni en variables de entorno ni en data/groq_key.txt",
            "has_key": False,
        }

    masked = key[:7] + "..." + key[-4:] if len(key) > 12 else "***"

    for model in MODELS_TO_TRY:
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": "Hola Robotina, responde con un JSON: {\"saludo\": \"ok\"}"}
            ],
            "max_tokens": 300,
        }
        try:
            req = urllib.request.Request(
                GROQ_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Robotina-Test/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                return {
                    "status": "success",
                    "model_used": model,
                    "masked_key": masked,
                    "response": content,
                }
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            logger.warning(f"Fallo modelo {model} con HTTP {e.code}: {err_body}")
        except Exception as e:
            logger.warning(f"Fallo modelo {model} con excepción: {e}")

    return {
        "status": "error",
        "masked_key": masked,
        "message": "Ninguno de los modelos de Groq respondió exitosamente.",
    }


def call_groq_clinical_parser(
    mensaje: str,
    camas_activas: list[dict[str, Any]],
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Interpreta cualquier mensaje informal/desestructurado de WhatsApp de un médico usando Groq."""
    key = get_groq_api_key(api_key)
    if not key:
        print("[GROQ AGENT]: ⚠ GROQ_API_KEY no configurada. Usando motor local de respaldo.", flush=True)
        return None

    # Resumen de camas para contexto clínico del prompt
    camas_ctx = "\n".join(
        [
            f"- Cama {c.get('cama')}: {c.get('nombre')} | Dx: {c.get('dx')} | Pendientes: {c.get('pendientes')}"
            for c in camas_activas
        ]
    )

    system_prompt = f"""Eres Robotina, asistente médica de guardia 24/7 en el hospital.
Los médicos residentes y tratantes te escriben por WhatsApp con lenguaje coloquial, rápido, abreviaturas médicas y sin estructura fija.
Tu objetivo es interpretar con precisión médica su intención clínica.

PACIENTES ACTUALES EN EL SERVICIO:
{camas_ctx}

INSTRUCCIONES CLÍNICAS:
1. 'intencion': Debe ser una de:
   - 'REGISTRAR_RESULTADO': Si el médico reporta un dato de laboratorio, gasometría, signos, imagen o resultado (ej: 'Robotina la gasometria de 6a es pf 6.9', 'el lactato de la 6a dio 4', 'ekg de 6d sin arritmias', 'cultivo de orina de la 8 negativo').
   - 'CREAR_PENDIENTE': Si el médico pide hacer una tarea o control (ej: 'anota gaso a las 21:30 para la 6a', 'ponle hemocultivos urgente al 8').
   - 'CREAR_RECORDATORIO': Si el médico pide recordar o programar una alarma/aviso a una hora determinada (ej: 'Robotina recuérdanos a las 7 el sodio del sr rodriguez', 'recuerda a las 21:30 la gaso de 6a', 'alarma a las 5 am para glicemia').
   - 'CONSULTAR_RECORDATORIOS': Si el médico pregunta por los recordatorios o alarmas activas (ej: 'Robotina recordatorios', 'qué alarmas hay programadas').
   - 'CONSULTAR_CAMA': Si pregunta por un paciente o cama (ej: 'cómo está la 6a', 'datos de don Rodriguez', 'qué tiene pendiente la 8').
   - 'CONSULTAR_RESUMEN': Si pide los pendientes de la noche o el checklist (ej: 'Robotina pendientes', 'resumen de guardia', 'qué hay para hoy').
   - 'CONSULTAR_CAMAS': Si pide la lista de camas censadas (ej: 'Robotina camas', 'qué pacientes tenemos').
   - 'GENERAR_PDF': Si pide generar o descargar la bitácora, el reporte o el PDF de la guardia (ej: 'Robotina genera la bitacora', 'Robotina genera el pdf', 'Robotina entrega de guardia', 'pasa el reporte').
   - 'AYUDA': Si saluda o pregunta qué haces (ej: 'Robotina ayuda', 'hola', 'qué comandos tienes').
   - 'OTRO': Si es un mensaje no relacionado.

2. 'cama': El código exacto de la cama (ej: '6A', '8', '6D', 'INFECTO 1B'). Si mencionan el apellido del paciente, asócialo a su cama respectiva. Si no aplica, null.
3. 'estudio': El nombre médico del examen o acción (ej: 'Gasometría arterial', 'Lactato', 'EKG', 'Hemocultivos', 'TAC de cráneo', 'Control de Sodio').
4. 'resultado': El valor numérico o reporte completo (ej: 'pf 6.9', 'Lactato 1.4 mmol/L', 'Ritmo sinusal normal').
5. 'horario': Horario si lo mencionan (ej: '07:00', '21:30', '05:00') o null.
6. 'prioridad': 'STAT' (si es crítico/inmediato), 'URGENTE', 'CONTROL' o 'RUTINA'.
7. 'comentario_clinico': Breve advertencia o nota médica si el resultado es crítico (ej: 'Acidosis metabólica severa / riesgo vital', 'Lactato elevado'). Si es normal, null.

Responde ÚNICAMENTE un objeto JSON válido con este formato:
{{
  "intencion": "REGISTRAR_RESULTADO" | "CREAR_PENDIENTE" | "CREAR_RECORDATORIO" | "CONSULTAR_RECORDATORIOS" | "CONSULTAR_CAMA" | "CONSULTAR_RESUMEN" | "CONSULTAR_CAMAS" | "GENERAR_PDF" | "AYUDA" | "OTRO",
  "cama": "6A",
  "estudio": "Gasometría arterial",
  "resultado": "pf 6.9",
  "prioridad": "STAT",
  "horario": null,
  "comentario_clinico": null
}}"""

    # Probar secuencialmente con los modelos disponibles en Groq
    for model in MODELS_TO_TRY:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": mensaje},
            ],
            "temperature": 0.1,
            "max_tokens": 800,
        }

        try:
            req = urllib.request.Request(
                GROQ_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Robotina-Clinical-Agent/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                m_json = re.search(r"\{[\s\S]*\}", content)
                if m_json:
                    parsed = json.loads(m_json.group(0))
                    print(f"[GROQ ÉXITO ({model})]: Intención detectada: {parsed.get('intencion')} | Cama: {parsed.get('cama')}", flush=True)
                    return parsed
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            print(f"[GROQ ERROR ({model}) HTTP {e.code}]: {err_msg}", flush=True)
        except Exception as e:
            print(f"[GROQ FALLO ({model})]: {e}", flush=True)

    return None
