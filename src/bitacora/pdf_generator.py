"""Generador automatizado de PDF y documentos Quarto para Robotina."""

import logging
import shutil
import subprocess
from pathlib import Path

from bitacora.bed_store import consolidar_bitacora
from bitacora.models import BitacoraGuardia
from bitacora.render_qmd import render_qmd

logger = logging.getLogger(__name__)


def generar_bitacora_completa(
    bitacora_path: Path | None = None,
    output_dir: Path = Path("reports"),
) -> dict[str, Path]:
    """Consolida las matrices individuales de cada cama y compila .qmd, .html y .pdf con Quarto."""
    output_dir.mkdir(parents=True, exist_ok=True)
    bitacora = consolidar_bitacora(sync_monolith_path=bitacora_path)

    qmd_path = output_dir / "bitacora_guardia.qmd"
    html_path = output_dir / "bitacora_guardia.html"
    pdf_path = output_dir / "bitacora_guardia.pdf"

    # 1. Generar instrumento Quarto (.qmd)
    render_qmd(bitacora, qmd_path)
    logger.info(f"Quarto QMD generado en {qmd_path}")

    # 2. Localizar binario de Quarto
    quarto_bin = (
        shutil.which("quarto")
        or shutil.which("/home/antigravity/opt/quarto/bin/quarto")
        or shutil.which("/usr/local/bin/quarto")
        or "quarto"
    )

    # 3. Compilar a HTML interactivo
    try:
        res_html = subprocess.run(
            [str(quarto_bin), "render", str(qmd_path), "--to", "html"],
            capture_output=True,
            text=True,
            timeout=40,
        )
        if res_html.returncode == 0:
            logger.info("Quarto HTML compilado exitosamente")
        else:
            logger.warning(f"Quarto render HTML advirtió: {res_html.stderr}")
    except Exception as e:
        logger.error(f"Error ejecutando Quarto render: {e}")

    # 4. Compilar a PDF vía Chromium/Chrome Headless
    chrome_bin = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or "/usr/bin/google-chrome"
    )

    if chrome_bin and html_path.exists():
        try:
            res_pdf = subprocess.run(
                [
                    str(chrome_bin),
                    "--headless",
                    "--disable-gpu",
                    "--no-sandbox",
                    f"--print-to-pdf={pdf_path}",
                    str(html_path.resolve()),
                ],
                capture_output=True,
                text=True,
                timeout=40,
            )
            if pdf_path.exists():
                logger.info(f"PDF generado exitosamente ({pdf_path.stat().st_size} bytes)")
        except Exception as e:
            logger.error(f"Error generando PDF con Chrome: {e}")

    # Respaldo: si ya existe bitacora_completa.pdf, copiarlo como fallback
    fallback_pdf = output_dir / "bitacora_completa.pdf"
    if not pdf_path.exists() and fallback_pdf.exists():
        shutil.copy(fallback_pdf, pdf_path)

    return {
        "qmd": qmd_path,
        "html": html_path,
        "pdf": pdf_path,
    }
