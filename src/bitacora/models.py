"""Modelos de datos para el sistema de Bitácora Médica de Guardia."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PendienteTipo(str, Enum):
    """Categorías funcionales de pendientes en la guardia hospitalaria."""

    LABORATORIO = "LABORATORIO"
    IMAGEN_PROCEDIMIENTO = "IMAGEN_PROCEDIMIENTO"
    INTERCONSULTA = "INTERCONSULTA"
    FARMACO_DOSIS = "FARMACO_DOSIS"
    CUIDADO_ENFERMERIA = "CUIDADO_ENFERMERIA"
    ADMINISTRATIVO_QX = "ADMINISTRATIVO_QX"


class PendientePrioridad(str, Enum):
    """Niveles de prioridad clínica del pendiente."""

    STAT = "STAT"  # Inmediato / Crítico (Gasometría STAT, EKG STAT)
    URGENTE = "URGENTE"  # En la visita nocturna (20:00 - 24:00)
    CONTROL = "CONTROL"  # En madrugada o ventana horaria (04:00 - 06:00 AM)
    RUTINA = "RUTINA"  # Turno de mañana o pase de visita


class TurnoObjetivo(str, Enum):
    """Momento operativo previsto para ejecutar o auditar el pendiente."""

    NOCTURNA_20_24 = "NOCTURNA_20_24"  # Visita nocturna
    MADRUGADA_04_06 = "MADRUGADA_04_06"  # Control de madrugada (ej. electrolitos 05:00)
    MANANA = "MANANA"  # Entrega de guardia / Mañana
    AL_ALTA = "AL_ALTA"
    CUALQUIERA = "CUALQUIERA"


class Pendiente(BaseModel):
    """Representa una tarea o pendiente clínico accionable durante la guardia."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Identificador único del pendiente dentro del paciente")
    tipo: PendienteTipo
    descripcion: str
    prioridad: PendientePrioridad = PendientePrioridad.RUTINA
    turno: TurnoObjetivo = TurnoObjetivo.NOCTURNA_20_24
    horario_programado: str | None = Field(
        default=None, description="Horario específico (ej. '05:00 AM', 'STAT', '18h00')"
    )
    es_visita_nocturna: bool = Field(
        default=True,
        description="Indica si debe revisarse en el checklist nocturno de la guardia",
    )
    estado: Literal["PENDIENTE", "EN_CURSO", "COMPLETADO", "CANCELADO"] = "PENDIENTE"
    responsable: str | None = None
    resultado_obtenido: str | None = None


class Medicacion(BaseModel):
    """Esquema de fármaco prescrito con control de antibióticos y suspensiones."""

    model_config = ConfigDict(frozen=True)

    nombre: str
    dosis: str
    via: str
    frecuencia: str
    fecha_inicio: str | None = None
    dias_tratamiento: str | None = Field(
        default=None, description="Contador de días de antibiótico (ej: '4/5', 'Día 10')"
    )
    indicacion_cie10: str | None = None
    suspendido: bool = False
    diferido: bool = False
    prn: bool = False
    nota: str | None = None


class MicrobiologiaCultivo(BaseModel):
    """Registro de aislamientos microbiológicos, cultivos y antibiogramas."""

    model_config = ConfigDict(frozen=True)

    fecha: str | None = None
    muestra: str
    microorganismo: str
    patron_resistencia: str | None = None  # ej. BLEE, Carbapenémico resistente
    sensible: list[str] = Field(default_factory=list)
    resistente: list[str] = Field(default_factory=list)
    recuento_colonias: str | None = None


class LaboratorioReciente(BaseModel):
    """Conjunto de resultados paraclínicos fechados."""

    model_config = ConfigDict(frozen=True)

    fecha_hora: str
    tipo_estudio: str  # BH, Quimica, Gasometria, Tiempos, etc.
    valores: dict[str, Any] = Field(default_factory=dict)
    alerta_critica: str | None = None


class Paciente(BaseModel):
    """Registro clínico de un paciente en la bitácora de guardia."""

    model_config = ConfigDict(frozen=True)

    cama: str
    area: Literal["HOSPITALIZACION", "PERIFERICOS", "EMERGENCIA_OBSERVACION"]
    servicio_tratante: str | None = None
    nombre_completo: str
    hc: str
    cedula: str | None = None
    edad: str
    sexo: str | None = None
    fecha_nacimiento: str | None = None
    fecha_ingreso: str | None = None
    dias_hospitalizacion: int | None = None

    # Antecedentes
    app: list[str] = Field(default_factory=list, description="Antecedentes patológicos personales")
    aqx: list[str] = Field(default_factory=list, description="Antecedentes quirúrgicos")
    alergias: list[str] = Field(default_factory=list)

    # Diagnósticos
    diagnosticos_activos: list[str] = Field(default_factory=list, description="En orden jerárquico")
    diagnosticos_pasivos: list[str] = Field(default_factory=list)

    # Plan
    dieta: str | None = None
    hidratacion: str | None = None
    ckd_epi: str | None = Field(default=None, description="TFG estimada en ml/min/1.73m2")
    medicaciones: list[Medicacion] = Field(default_factory=list)

    # Pendientes y observaciones
    pendientes: list[Pendiente] = Field(default_factory=list)
    observaciones: list[str] = Field(default_factory=list)

    # Paraclínicos
    laboratorios: list[LaboratorioReciente] = Field(default_factory=list)
    cultivos: list[MicrobiologiaCultivo] = Field(default_factory=list)
    alertas_criticas: list[str] = Field(default_factory=list)


class GuardiaCenso(BaseModel):
    """Estadística de ocupación durante la guardia."""

    model_config = ConfigDict(frozen=True)

    total_pacientes: int
    emergencia: int
    hospitalizados: int
    perifericos: int
    ingresos_guardia: int = 0


class BitacoraGuardia(BaseModel):
    """Modelo raíz del documento de bitácora médica de guardia."""

    model_config = ConfigDict(frozen=True)

    version_schema: str = "1.0.0"
    fecha_guardia: str  # ej. "2026-11-09"
    periodo: str  # ej. "09/11/2026 AL 10/11/2026"
    medicos_guardia: list[str]
    censo: GuardiaCenso
    enlaces_utiles: dict[str, str] = Field(default_factory=dict)
    pacientes: list[Paciente] = Field(default_factory=list)
