"""Módulo de Inteligencia Clínica con Groq (Gemma / Llama) para Robotina."""

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def call_groq_clinical_parser(
    mensaje: str,
    camas_activas: list[dict[str, Any]],
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Interpreta cualquier mensaje informal/desestructurado de WhatsApp de un médico usando Groq.

    Devuelve un diccionario estructurado con:
    - intencion: REGISTRAR_RESULTADO | CREAR_PENDIENTE | CONSULTAR_CAMA | CONSULTAR_RESUMEN | CONSULTAR_CAMAS | AYUDA | OTRO
    - cama: str
    - estudio: str
    - resultado: str
    - prioridad: STAT | URGENTE | CONTROL | RUTINA
    - horario: str | None
    - comentario_clinico: str | None
    """
    key = api_key or os.getenv("GROQ_API_KEY")
    if not key:
        return None

    # Resumen de camas para contexto clínico
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
   - 'CONSULTAR_CAMA': Si pregunta por un paciente o cama (ej: 'cómo está la 6a', 'datos de don Rodriguez', 'qué tiene pendiente la 8').
   - 'CONSULTAR_RESUMEN': Si pide los pendientes de la noche o el checklist (ej: 'Robotina pendientes', 'resumen de guardia', 'qué hay para hoy').
   - 'CONSULTAR_CAMAS': Si pide la lista de camas censadas (ej: 'Robotina camas', 'qué pacientes tenemos').
   - 'GENERAR_PDF': Si pide generar o descargar la bitácora, el reporte o el PDF de la guardia (ej: 'Robotina genera la bitacora', 'Robotina genera el pdf', 'Robotina entrega de guardia', 'pasa el reporte').
   - 'AYUDA': Si saluda o pregunta qué haces (ej: 'Robotina ayuda', 'hola', 'qué comandos tienes').
   - 'OTRO': Si es un mensaje no relacionado.

2. 'cama': El código exacto de la cama (ej: '6A', '8', '6D', 'INFECTO 1B'). Si mencionan el apellido del paciente, asócialo a su cama respectiva. Si no aplica, null.
3. 'estudio': El nombre médico del examen o acción (ej: 'Gasometría arterial', 'Lactato', 'EKG', 'Hemocultivos', 'TAC de cráneo').
4. 'resultado': El valor numérico o reporte completo (ej: 'pf 6.9', 'Lactato 1.4 mmol/L', 'Ritmo sinusal normal').
5. 'prioridad': 'STAT' (si es crítico/inmediato), 'URGENTE', 'CONTROL' o 'RUTINA'.
6. 'comentario_clinico': Breve advertencia o nota médica si el resultado es crítico (ej: 'Acidosis metabólica severa / riesgo vital', 'Lactato elevado'). Si es normal, null.

Responde ÚNICAMENTE un objeto JSON válido con este formato:
{{
  "intencion": "REGISTRAR_RESULTADO" | "CREAR_PENDIENTE" | "CONSULTAR_CAMA" | "CONSULTAR_RESUMEN" | "CONSULTAR_CAMAS" | "AYUDA" | "OTRO",
  "cama": "6A",
  "estudio": "Gasometría arterial",
  "resultado": "pf 6.9",
  "prioridad": "STAT",
  "horario": null,
  "comentario_clinico": null
}}"""

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": mensaje},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "max_tokens": 400,
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
            parsed = json.loads(content)
            return parsed
    except Exception as e:
        logger.warning(f"Llamada a Groq API no disponible en este entorno: {e}")
        return None
