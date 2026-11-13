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
    """Orquestador clínico con Groq: decide qué script Python ejecutar o responde directamente."""
    key = get_groq_api_key(api_key)
    if not key:
        print("[GROQ AGENT]: ⚠ GROQ_API_KEY no configurada. Usando motor local de respaldo.", flush=True)
        return None

    # Resumen detallado de camas para contexto clínico del prompt
    if camas_activas:
        camas_ctx = "\n".join(
            [
                f"- Cama {c.get('cama')} ({c.get('area', '')}): {c.get('nombre')} ({c.get('edad', 'N/D')} años) | "
                f"Dx: {c.get('dx')} | Meds: {c.get('meds', 'N/D')} | Labs: {c.get('labs', 'N/D')} | "
                f"Obs/Evol: {c.get('obs', 'N/D')} | Alertas: {c.get('alertas', 'N/D')} | Pendientes: {c.get('pendientes', 'N/D')}"
                for c in camas_activas
            ]
        )
    else:
        camas_ctx = "(Todas las camas están actualmente LIBRES)"

    system_prompt = f"""Eres Robotina, la jefa clínica y orquestadora IA de la guardia médica 24/7 en WhatsApp.
Los médicos residentes y tratantes te escriben con lenguaje natural, coloquial y abreviaturas médicas.
Tu trabajo es DECIDIR qué acción/script del sistema corresponde ejecutar, o responder directamente una pregunta clínica puntual usando los datos reales de las camas.

MATRIZ ACTUAL DE CAMAS OCUPADAS (cada cama es un archivo independiente; si una cama no aparece aquí, está LIBRE):
{camas_ctx}

ACCIONES / SCRIPTS DISPONIBLES (elige exactamente una en 'intencion'):
1. 'EGRESAR_PACIENTE': Sacar, dar de alta, egresar o liberar una cama (ej: "saca al paciente de 6a", "egreso de rodriguez", "alta de 6a", "libera la 8").
2. 'INGRESAR_PACIENTE': Ingresar o asignar un paciente nuevo a una cama (ej: "ingresa en 6a a Maria Lopez de 45 años con dx neumonia", "nuevo paciente en 7: Carlos Ruiz 60a, HTA").
3. 'ACTUALIZAR_EVOLUCION': Agregar nota de evolución, signos vitales, estado clínico o diagnóstico a un paciente (ej: "evolucion de 6a: paciente estable afebril", "anota en 6a que inició noradrenalina").
4. 'REGISTRAR_RESULTADO': Reportar un resultado de laboratorio, gasometría, imagen o EKG (ej: "la gaso de 6a dio ph 6.9", "creatinina de 8 en 1.2").
5. 'CREAR_PENDIENTE': Anotar un nuevo pendiente o tarea clínica en una cama (ej: "anota hemocultivos urgente para 6a", "ponle control de sodio 05:00 a la 8").
6. 'COMPLETAR_PENDIENTE': Marcar un pendiente como hecho/completado sin resultado numérico (ej: "ya se hizo el ekg de 6a", "completa la interconsulta de 6a").
7. 'CREAR_RECORDATORIO': Programar una alarma/recordatorio con hora específica (ej: "recuerdanos a las 7 el sodio de rodriguez", "alarma 21:30 gaso de 6a").
8. 'CONSULTAR_RECORDATORIOS': Listar alarmas/recordatorios activos.
9. 'CONSULTAR_CAMA': Ver la ficha clínica completa de una cama o paciente (ej: "cómo está la 6a", "datos de rodriguez", "qué exámenes tiene 6a").
10. 'CONSULTAR_RESUMEN': Ver el checklist nocturno / resumen de pendientes de toda la guardia (ej: "pendientes", "resumen de guardia").
11. 'CONSULTAR_CAMAS': Ver la lista de camas ocupadas actualmente (ej: "camas", "qué pacientes tenemos").
12. 'GENERAR_PDF': Consolidar todas las matrices de camas y compilar la bitácora oficial en PDF/HTML con Quarto (ej: "genera el pdf", "bitacora con quarto", "entrega de guardia").
13. 'RESPUESTA_DIRECTA': Cuando el médico hace una pregunta clínica específica que puedes responder directo con los datos de arriba (ej: "qué antibiótico tiene rodriguez?", "quién está en periféricos?", "qué pacientes tienen leucopenia?", "cuánto tuvo de plaquetas 6a?"). En este caso llena 'respuesta_directa' con una respuesta clara, breve y formateada para WhatsApp.
14. 'AYUDA': Saludo o pregunta sobre qué puedes hacer.
15. 'OTRO': Conversación social entre médicos que NO va dirigida a ti ni requiere acción clínica.

CAMPOS DEL JSON:
- "intencion": Una de las 15 opciones exactas.
- "cama": Código exacto de la cama (ej: "6A", "8", "INFECTO 1B"). Si mencionan un apellido, asócialo a su cama. Si no aplica, null.
- "paciente": Nombre completo del paciente (especialmente en INGRESAR_PACIENTE) o null.
- "edad": Edad en texto si se menciona en INGRESAR_PACIENTE (ej: "45") o null.
- "area": "HOSPITALIZACION" | "PERIFERICOS" | "EMERGENCIA_OBSERVACION" (para INGRESAR_PACIENTE si se deduce, por defecto "HOSPITALIZACION").
- "diagnostico": Diagnóstico si se menciona en ingreso o evolución, o null.
- "estudio": Nombre del estudio, pendiente o tarea (o motivo de egreso en EGRESAR_PACIENTE).
- "resultado": Valor del resultado reportado o null.
- "nota": Nota de evolución u observación clínica (para ACTUALIZAR_EVOLUCION) o null.
- "prioridad": "STAT" | "URGENTE" | "CONTROL" | "RUTINA".
- "horario": Hora en formato "HH:MM" si aplica (ej: "19:00", "07:00", "21:30") o null.
- "comentario_clinico": Breve alerta médica si un resultado es crítico, o null.
- "respuesta_directa": Texto listo para WhatsApp si intencion == "RESPUESTA_DIRECTA", o null.

Responde ÚNICAMENTE con un objeto JSON válido."""

    # Probar secuencialmente con los modelos disponibles en Groq
    for model in MODELS_TO_TRY:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": mensaje},
            ],
            "temperature": 0.1,
            "max_tokens": 900,
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
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                m_json = re.search(r"\{[\s\S]*\}", content)
                if m_json:
                    parsed = json.loads(m_json.group(0))
                    print(
                        f"[GROQ ÉXITO ({model})]: Script={parsed.get('intencion')} | Cama={parsed.get('cama')}",
                        flush=True,
                    )
                    return parsed
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            print(f"[GROQ ERROR ({model}) HTTP {e.code}]: {err_msg}", flush=True)
        except Exception as e:
            print(f"[GROQ FALLO ({model})]: {e}", flush=True)

    return None
