"""Gestión de matriz individual por cama (data/camas/*.json) y consolidación para Quarto."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from bitacora.models import (
    BitacoraGuardia,
    GuardiaCenso,
    LaboratorioReciente,
    Paciente,
    Pendiente,
    PendientePrioridad,
    PendienteTipo,
    TurnoObjetivo,
)
from bitacora.schema import load_bitacora, save_bitacora

logger = logging.getLogger(__name__)

CAMAS_DIR = Path("data/camas")
HISTORIAL_DIR = Path("data/historial_egresos")
META_FILE = Path("data/guardia_meta.json")


def _safe_filename(cama: str) -> str:
    """Convierte un código de cama (ej: 'INFECTO 1B', '6A') en nombre de archivo seguro."""
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", cama.strip().upper()).strip("_")
    return f"{clean}.json"


def get_latest_monolith_path() -> Path:
    """Busca el archivo bitacora_*.json más reciente."""
    candidates = sorted(Path("data").glob("bitacora_*.json"))
    if candidates:
        return candidates[-1]
    return Path("data/bitacora_2026_11_10.json")


def ensure_bed_store_initialized(monolith_path: Path | None = None) -> None:
    """Inicializa data/camas/*.json y data/guardia_meta.json desde la bitácora si aún no existen."""
    CAMAS_DIR.mkdir(parents=True, exist_ok=True)
    HISTORIAL_DIR.mkdir(parents=True, exist_ok=True)

    existing_beds = list(CAMAS_DIR.glob("*.json"))
    if existing_beds and META_FILE.exists():
        return

    src_path = monolith_path or get_latest_monolith_path()
    if not src_path.exists():
        if not META_FILE.exists():
            META_FILE.write_text(
                json.dumps(
                    {
                        "version_schema": "1.0.0",
                        "fecha_guardia": datetime.now().strftime("%Y-%m-%d"),
                        "periodo": datetime.now().strftime("%d/%m/%Y"),
                        "medicos_guardia": ["Equipo de Guardia"],
                        "enlaces_utiles": {},
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return

    bitacora = load_bitacora(src_path)

    # Guardar metadatos de la guardia
    meta = {
        "version_schema": bitacora.version_schema,
        "fecha_guardia": bitacora.fecha_guardia,
        "periodo": bitacora.periodo,
        "medicos_guardia": bitacora.medicos_guardia,
        "enlaces_utiles": bitacora.enlaces_utiles,
        "ingresos_guardia": bitacora.censo.ingresos_guardia,
    }
    META_FILE.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # Crear matriz individual por cada cama ocupada
    if not existing_beds:
        for pac in bitacora.pacientes:
            save_cama(pac)
        logger.info(f"Matriz individual inicializada con {len(bitacora.pacientes)} camas en {CAMAS_DIR}")


def save_cama(paciente: Paciente) -> Path:
    """Guarda o actualiza el archivo JSON individual de una cama."""
    CAMAS_DIR.mkdir(parents=True, exist_ok=True)
    file_path = CAMAS_DIR / _safe_filename(paciente.cama)
    file_path.write_text(
        paciente.model_dump_json(indent=2, by_alias=True),
        encoding="utf-8",
    )
    return file_path


def get_cama(cama_id: str) -> Paciente | None:
    """Lee el archivo individual de una cama específica."""
    ensure_bed_store_initialized()
    target = cama_id.strip().upper()
    direct_path = CAMAS_DIR / _safe_filename(target)
    if direct_path.exists():
        try:
            return Paciente.model_validate_json(direct_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error leyendo cama {direct_path}: {e}")

    # Búsqueda flexible si hay diferencia de espacios/guiones
    for p_file in sorted(CAMAS_DIR.glob("*.json")):
        try:
            pac = Paciente.model_validate_json(p_file.read_text(encoding="utf-8"))
            if pac.cama.strip().upper() == target:
                return pac
        except Exception:
            continue
    return None


def list_camas_activas() -> list[Paciente]:
    """Lista todos los pacientes leyendo cada archivo individual en data/camas/*.json."""
    ensure_bed_store_initialized()
    pacientes: list[Paciente] = []
    for p_file in sorted(CAMAS_DIR.glob("*.json")):
        try:
            pac = Paciente.model_validate_json(p_file.read_text(encoding="utf-8"))
            pacientes.append(pac)
        except Exception as e:
            logger.error(f"Error cargando archivo de cama {p_file}: {e}")
    return pacientes


def consolidar_bitacora(sync_monolith_path: Path | None = None) -> BitacoraGuardia:
    """Consolida todas las matrices individuales de camas en una BitacoraGuardia unificada para Quarto."""
    ensure_bed_store_initialized(sync_monolith_path)
    meta = json.loads(META_FILE.read_text(encoding="utf-8")) if META_FILE.exists() else {}
    pacientes = list_camas_activas()

    hosp = sum(1 for p in pacientes if p.area == "HOSPITALIZACION")
    perif = sum(1 for p in pacientes if p.area == "PERIFERICOS")
    emerg = sum(1 for p in pacientes if p.area == "EMERGENCIA_OBSERVACION")

    censo = GuardiaCenso(
        total_pacientes=len(pacientes),
        emergencia=emerg,
        hospitalizados=hosp,
        perifericos=perif,
        ingresos_guardia=int(meta.get("ingresos_guardia", 0)),
    )

    bitacora = BitacoraGuardia(
        version_schema=meta.get("version_schema", "1.0.0"),
        fecha_guardia=meta.get("fecha_guardia", datetime.now().strftime("%Y-%m-%d")),
        periodo=meta.get("periodo", datetime.now().strftime("%d/%m/%Y")),
        medicos_guardia=meta.get("medicos_guardia", ["Equipo de Guardia"]),
        censo=censo,
        enlaces_utiles=meta.get("enlaces_utiles", {}),
        pacientes=pacientes,
    )

    target_sync = sync_monolith_path or get_latest_monolith_path()
    save_bitacora(bitacora, target_sync)
    return bitacora


def egresar_paciente_cama(cama_id: str, motivo: str = "Alta / Egreso") -> dict[str, Any]:
    """Saca/egresa al paciente de una cama, archiva su matriz individual y libera la cama."""
    ensure_bed_store_initialized()
    pac = get_cama(cama_id)
    if not pac:
        return {"ok": False, "reason": f"La cama {cama_id.upper()} ya está libre o no tiene paciente asignado."}

    HISTORIAL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = HISTORIAL_DIR / f"{_safe_filename(pac.cama).replace('.json', '')}_{ts}.json"

    archive_data = pac.model_dump(mode="json")
    archive_data["egreso_motivo"] = motivo
    archive_data["egreso_fecha_hora"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    archive_path.write_text(json.dumps(archive_data, indent=2, ensure_ascii=False), encoding="utf-8")

    bed_file = CAMAS_DIR / _safe_filename(pac.cama)
    if bed_file.exists():
        bed_file.unlink()
    else:
        for p_file in CAMAS_DIR.glob("*.json"):
            try:
                loaded = Paciente.model_validate_json(p_file.read_text(encoding="utf-8"))
                if loaded.cama.strip().upper() == pac.cama.strip().upper():
                    p_file.unlink()
                    break
            except Exception:
                continue

    consolidar_bitacora()
    return {
        "ok": True,
        "cama": pac.cama,
        "paciente": pac.nombre_completo,
        "motivo": motivo,
        "archivo": str(archive_path),
    }


def ingresar_paciente_cama(
    cama_id: str,
    nombre_completo: str,
    edad: str = "N/D",
    area: Literal["HOSPITALIZACION", "PERIFERICOS", "EMERGENCIA_OBSERVACION"] = "HOSPITALIZACION",
    diagnostico: str | None = None,
    hc: str = "N/D",
    observacion: str | None = None,
) -> dict[str, Any]:
    """Ingresa un nuevo paciente en su matriz individual de cama."""
    ensure_bed_store_initialized()
    cama_norm = cama_id.strip().upper()
    existente = get_cama(cama_norm)
    if existente:
        # Archivar automáticamente el paciente anterior si existía
        egresar_paciente_cama(cama_norm, motivo="Reemplazo por nuevo ingreso")

    nuevo = Paciente(
        cama=cama_norm,
        area=area,
        nombre_completo=nombre_completo.strip().upper(),
        hc=hc,
        edad=str(edad),
        fecha_ingreso=datetime.now().strftime("%Y-%m-%d"),
        diagnosticos_activos=[diagnostico] if diagnostico else [],
        observaciones=[observacion] if observacion else [],
    )
    save_cama(nuevo)

    # Incrementar contador de ingresos en metadata
    if META_FILE.exists():
        meta = json.loads(META_FILE.read_text(encoding="utf-8"))
        meta["ingresos_guardia"] = int(meta.get("ingresos_guardia", 0)) + 1
        META_FILE.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    consolidar_bitacora()
    return {"ok": True, "paciente": nuevo}


def actualizar_evolucion_cama(
    cama_id: str,
    nota: str,
    diagnostico: str | None = None,
) -> dict[str, Any]:
    """Agrega una nota de evolución u observación clínica a la matriz individual de la cama."""
    pac = get_cama(cama_id)
    if not pac:
        return {"ok": False, "reason": f"La cama {cama_id.upper()} está libre (no hay paciente)."}

    ts = datetime.now().strftime("%d/%m %H:%M")
    nuevas_obs = list(pac.observaciones) + [f"[{ts}] {nota.strip()}"]
    nuevos_dx = list(pac.diagnosticos_activos)
    if diagnostico and diagnostico.strip() and diagnostico.strip() not in nuevos_dx:
        nuevos_dx.insert(0, diagnostico.strip())

    pac_actualizado = pac.model_copy(
        update={
            "observaciones": nuevas_obs,
            "diagnosticos_activos": nuevos_dx,
        }
    )
    save_cama(pac_actualizado)
    consolidar_bitacora()
    return {"ok": True, "paciente": pac_actualizado, "nota": f"[{ts}] {nota.strip()}"}


def registrar_resultado_cama(
    cama_id: str,
    estudio: str,
    resultado: str,
    alerta: str | None = None,
) -> dict[str, Any]:
    """Registra un resultado clínico en la matriz individual de la cama (pendiente + laboratorio)."""
    pac = get_cama(cama_id)
    if not pac:
        return {"ok": False, "reason": f"La cama {cama_id.upper()} está libre (no hay paciente)."}

    estudio_clean = estudio.strip() or "Reporte"
    val_clean = resultado.strip()
    modificado = False
    pend_desc = ""
    pends_nuevos: list[Pendiente] = []

    for pend in pac.pendientes:
        coincide = bool(
            estudio_clean.lower() in pend.descripcion.lower()
            or (estudio_clean.lower().startswith("gas") and "gas" in pend.descripcion.lower())
        )
        if pend.es_visita_nocturna and pend.estado == "PENDIENTE" and (coincide or not modificado):
            pends_nuevos.append(
                pend.model_copy(update={"estado": "COMPLETADO", "resultado_obtenido": val_clean})
            )
            modificado = True
            pend_desc = pend.descripcion
        else:
            pends_nuevos.append(pend)

    if not modificado:
        nuevo_desc = f"{estudio_clean} (WhatsApp)"
        nuevo_p = Pendiente(
            id=f"PEND-{pac.cama}-{len(pac.pendientes) + 1:02d}",
            tipo=PendienteTipo.LABORATORIO,
            descripcion=nuevo_desc,
            prioridad=PendientePrioridad.STAT,
            turno=TurnoObjetivo.NOCTURNA_20_24,
            es_visita_nocturna=True,
            estado="COMPLETADO",
            resultado_obtenido=val_clean,
        )
        pends_nuevos.append(nuevo_p)
        pend_desc = nuevo_desc

    nuevo_lab = LaboratorioReciente(
        fecha_hora=datetime.now().strftime("%Y-%m-%d %H:%M"),
        tipo_estudio=estudio_clean,
        valores={"resultado": val_clean},
        alerta_critica=alerta,
    )
    nuevos_labs = list(pac.laboratorios) + [nuevo_lab]
    nuevas_alertas = list(pac.alertas_criticas)
    if alerta and alerta not in nuevas_alertas:
        nuevas_alertas.append(alerta)

    pac_actualizado = pac.model_copy(
        update={
            "pendientes": pends_nuevos,
            "laboratorios": nuevos_labs,
            "alertas_criticas": nuevas_alertas,
        }
    )
    save_cama(pac_actualizado)
    consolidar_bitacora()
    return {"ok": True, "paciente": pac_actualizado, "pend_desc": pend_desc}


def crear_pendiente_cama(
    cama_id: str,
    descripcion: str,
    prioridad: str = "URGENTE",
    horario: str | None = None,
) -> dict[str, Any]:
    """Agrega un nuevo pendiente a la matriz individual de la cama."""
    pac = get_cama(cama_id)
    if not pac:
        return {"ok": False, "reason": f"La cama {cama_id.upper()} está libre (no hay paciente)."}

    prio_map = {
        "STAT": PendientePrioridad.STAT,
        "URGENTE": PendientePrioridad.URGENTE,
        "CONTROL": PendientePrioridad.CONTROL,
        "RUTINA": PendientePrioridad.RUTINA,
    }
    prio_enum = prio_map.get((prioridad or "URGENTE").upper(), PendientePrioridad.URGENTE)

    nuevo_id = f"PEND-{pac.cama}-{len(pac.pendientes) + 1:02d}"
    nuevo_pend = Pendiente(
        id=nuevo_id,
        tipo=PendienteTipo.LABORATORIO,
        descripcion=descripcion.strip(),
        prioridad=prio_enum,
        turno=TurnoObjetivo.NOCTURNA_20_24,
        horario_programado=horario,
        es_visita_nocturna=True,
        estado="PENDIENTE",
    )
    pac_actualizado = pac.model_copy(update={"pendientes": list(pac.pendientes) + [nuevo_pend]})
    save_cama(pac_actualizado)
    consolidar_bitacora()
    return {"ok": True, "paciente": pac_actualizado, "pendiente": nuevo_pend}


def completar_pendiente_cama(
    cama_id: str,
    estudio: str = "",
    resultado: str | None = None,
) -> dict[str, Any]:
    """Marca un pendiente existente en la cama como COMPLETADO."""
    pac = get_cama(cama_id)
    if not pac:
        return {"ok": False, "reason": f"La cama {cama_id.upper()} está libre (no hay paciente)."}

    pends_nuevos: list[Pendiente] = []
    completado_desc = None
    kw = estudio.strip().lower()

    for pend in pac.pendientes:
        if pend.estado == "PENDIENTE" and (not kw or kw in pend.descripcion.lower()) and completado_desc is None:
            pends_nuevos.append(
                pend.model_copy(
                    update={
                        "estado": "COMPLETADO",
                        "resultado_obtenido": resultado or pend.resultado_obtenido or "Realizado",
                    }
                )
            )
            completado_desc = pend.descripcion
        else:
            pends_nuevos.append(pend)

    if not completado_desc:
        return {"ok": False, "reason": f"No se encontró un pendiente activo que coincida con '{estudio}' en la cama {pac.cama}."}

    pac_actualizado = pac.model_copy(update={"pendientes": pends_nuevos})
    save_cama(pac_actualizado)
    consolidar_bitacora()
    return {"ok": True, "paciente": pac_actualizado, "descripcion": completado_desc}
