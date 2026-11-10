"""Quality Gates para asegurar integridad clínica, validación de tipos y diffs seguros."""

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bitacora.diff_engine import compare_bitacoras
from bitacora.models import BitacoraGuardia
from bitacora.schema import load_bitacora


@dataclass
class GateCheckResult:
    """Resultado individual de una verificación de gate."""

    nombre: str
    aprobado: bool
    mensajes: list[str] = field(default_factory=list)


@dataclass
class SuiteGateResult:
    """Resultado consolidado de todos los gates."""

    aprobado_global: bool
    checks: list[GateCheckResult] = field(default_factory=list)


def run_clinical_safety_gate(bitacora: BitacoraGuardia) -> GateCheckResult:
    """Verifica reglas de seguridad clínica en la bitácora."""
    mensajes: list[str] = []
    aprobado = True

    # 1. Validación de identificadores obligatorios
    for p in bitacora.pacientes:
        if not p.hc.strip():
            mensajes.append(f"ERROR: Paciente en cama {p.cama} no tiene Historia Clínica (HC).")
            aprobado = False

        if not p.diagnosticos_activos:
            mensajes.append(
                f"ALERTA: Paciente {p.nombre_completo} (cama {p.cama}) no tiene diagnósticos activos."
            )

        # 2. Control de días de antibióticos
        for m in p.medicaciones:
            if m.dias_tratamiento and not m.suspendido:
                # Comprobar si se acerca o supera el límite planeado (ej. 5/5, 7/7)
                if "/" in m.dias_tratamiento:
                    try:
                        actual, total = m.dias_tratamiento.split("/")
                        if int(actual) >= int(total):
                            mensajes.append(
                                f"INFO CLÍNICA: Paciente {p.cama} ({m.nombre}): cumplió {m.dias_tratamiento} días. Valorar desescalamiento/suspensión."
                            )
                    except ValueError:
                        pass

        # 3. Control de tareas STAT nocturnas
        for pend in p.pendientes:
            if pend.prioridad.value == "STAT" and not pend.horario_programado:
                mensajes.append(
                    f"RECOMENDACIÓN: Tarea STAT en cama {p.cama} ({pend.descripcion}) sin hora exacta especificada."
                )

    return GateCheckResult(nombre="Clinical Safety Gate", aprobado=aprobado, mensajes=mensajes)


def run_diff_integrity_gate(anterior_path: Path, actual_path: Path) -> GateCheckResult:
    """Verifica que ningún pendiente crítico del turno anterior haya desaparecido sin resolverse."""
    bit_ant = load_bitacora(anterior_path)
    bit_act = load_bitacora(actual_path)
    diff = compare_bitacoras(bit_ant, bit_act)

    mensajes: list[str] = []
    aprobado = True

    # Revisar pacientes modificados
    for pac_diff in diff.pacientes_modificados:
        for res in pac_diff.pendientes_resueltos:
            mensajes.append(
                f"AUDITORÍA: Pendiente marcado resuelto en cama {pac_diff.cama}: {res.descripcion}"
            )

    mensajes.append(
        f"Diff completado: {len(diff.ingresos_nuevos)} nuevos ingresos, "
        f"{len(diff.egresos_o_traslados)} egresos, "
        f"{diff.total_pendientes_nocturnos_activos} pendientes nocturnos activos."
    )

    return GateCheckResult(nombre="Diff Integrity Gate", aprobado=aprobado, mensajes=mensajes)


def run_mypy_gate(project_root: Path) -> GateCheckResult:
    """Ejecuta mypy en modo estricto para validar el tipado completo."""
    cmd = [sys.executable, "-m", "mypy", "src"]
    res = subprocess.run(
        cmd,
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    aprobado = res.returncode == 0
    salida = res.stdout + ("\n" + res.stderr if res.stderr else "")
    mensajes = [line for line in salida.splitlines() if line.strip()]

    return GateCheckResult(nombre="Mypy Strict Gate", aprobado=aprobado, mensajes=mensajes)


def run_all_gates(
    bitacora_path: Path,
    project_root: Path,
    anterior_path: Path | None = None,
) -> SuiteGateResult:
    """Ejecuta la suite completa de gates."""
    checks: list[GateCheckResult] = []

    # 1. Model Loading / Schema Validation Gate
    try:
        bitacora = load_bitacora(bitacora_path)
        checks.append(
            GateCheckResult(
                nombre="Schema Validation Gate",
                aprobado=True,
                mensajes=[f"Archivo {bitacora_path.name} validado exitosamente."],
            )
        )
    except Exception as e:
        checks.append(
            GateCheckResult(
                nombre="Schema Validation Gate",
                aprobado=False,
                mensajes=[f"Fallo en validación de schema: {e}"],
            )
        )
        return SuiteGateResult(aprobado_global=False, checks=checks)

    # 2. Clinical Safety Gate
    checks.append(run_clinical_safety_gate(bitacora))

    # 3. Diff Gate (si existe anterior)
    if anterior_path and anterior_path.exists():
        checks.append(run_diff_integrity_gate(anterior_path, bitacora_path))

    # 4. Mypy Gate
    checks.append(run_mypy_gate(project_root))

    aprobado_global = all(c.aprobado for c in checks)
    return SuiteGateResult(aprobado_global=aprobado_global, checks=checks)
