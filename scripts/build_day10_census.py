"""Genera la bitácora del 10/11/2026 correspondiente al censo completo de 26 pacientes."""

import json
from pathlib import Path

bit_09 = json.loads(Path("data/bitacora_2026_11_09.json").read_text(encoding="utf-8"))

pacientes_10 = []
for p in bit_09["pacientes"]:
    p_copy = dict(p)
    if p["cama"] == "6D":
        # Paciente Intriago egresó, sustituido por nuevo ingreso
        pacientes_10.append({
            "cama": "6D",
            "area": "HOSPITALIZACION",
            "servicio_tratante": "Medicina Interna",
            "nombre_completo": "LOPEZ PAZMINO RICARDO DARIO",
            "hc": "2199041",
            "cedula": "1711223344",
            "edad": "48 años",
            "sexo": "M",
            "app": ["Hipertensión arterial"],
            "aqx": ["Apendicectomía"],
            "alergias": ["PENICILINA"],
            "diagnosticos_activos": ["Crisis hipertensiva tipo urgencia"],
            "diagnosticos_pasivos": [],
            "dieta": "Hiposódica estricta",
            "hidratacion": "Vía salinizada",
            "ckd_epi": "95 ml/min/1.73m2",
            "medicaciones": [
                {
                    "nombre": "Amlodipino",
                    "dosis": "10 mg",
                    "via": "VO",
                    "frecuencia": "cada día",
                }
            ],
            "pendientes": [
                {
                    "id": "PEND-6D-NUEVO-01",
                    "tipo": "LABORATORIO",
                    "descripcion": "Segunda curva de Troponina ultrasensible a las 22:00 + EKG",
                    "prioridad": "STAT",
                    "turno": "NOCTURNA_20_24",
                    "horario_programado": "22:00",
                    "es_visita_nocturna": True,
                    "estado": "PENDIENTE",
                }
            ],
            "observaciones": ["Nuevo ingreso de guardia."],
            "laboratorios": [],
            "cultivos": [],
            "alertas_criticas": ["Alergia a PENICILINA"],
        })
    elif p["cama"] == "6A":
        # 6A completó sus pendientes nocturnos y avanzó antibiótico a 5/5
        p_copy["pendientes"] = [
            {
                "id": "PEND-6A-01",
                "tipo": "IMAGEN_PROCEDIMIENTO",
                "descripcion": "Electrocardiograma (EKG) STAT",
                "prioridad": "STAT",
                "turno": "NOCTURNA_20_24",
                "es_visita_nocturna": False,
                "estado": "COMPLETADO",
                "resultado_obtenido": "Ritmo sinusal normal",
            },
            {
                "id": "PEND-6A-02",
                "tipo": "LABORATORIO",
                "descripcion": "Gasometría arterial STAT + Electrolitos",
                "prioridad": "STAT",
                "turno": "NOCTURNA_20_24",
                "es_visita_nocturna": False,
                "estado": "COMPLETADO",
                "resultado_obtenido": "Gases normales, lactato 1.1",
            },
            {
                "id": "PEND-6A-04",
                "tipo": "FARMACO_DOSIS",
                "descripcion": "Evaluar fin de ciclo de antibiótico (Día 5/5)",
                "prioridad": "URGENTE",
                "turno": "NOCTURNA_20_24",
                "es_visita_nocturna": True,
                "estado": "PENDIENTE",
            },
        ]
        # Modificar contador de antibiótico a 5/5
        meds = []
        for m in p_copy["medicaciones"]:
            m_dict = dict(m)
            if "Ampicilina" in m_dict["nombre"]:
                m_dict["dias_tratamiento"] = "5/5"
            meds.append(m_dict)
        p_copy["medicaciones"] = meds
        pacientes_10.append(p_copy)
    else:
        pacientes_10.append(p_copy)

bit_10 = {
    "version_schema": "1.0.0",
    "fecha_guardia": "2026-11-10",
    "periodo": "10/11/2026 AL 11/11/2026",
    "medicos_guardia": [
        "MD MORAN (PG Reumatología IRM)",
        "MD GOMEZ (PG Medicina Interna)",
    ],
    "censo": {
        "total_pacientes": 42,
        "hospitalizados": 18,
        "perifericos": 12,
        "emergencia": 12,
        "ingresos_guardia": 1,
    },
    "enlaces_utiles": bit_09["enlaces_utiles"],
    "pacientes": pacientes_10,
}

Path("data/bitacora_2026_11_10.json").write_text(
    json.dumps(bit_10, indent=2, ensure_ascii=False), encoding="utf-8"
)
print("bitacora_2026_11_10.json actualizada exitosamente.")

