"""
MICROSERVICIO JURÍDICO — v6.0
El sistema analiza y distribuye; el abogado decide.

Ya NO emite veredicto APROBADO/DESAPROBADO. Cada análisis se guarda en la carpeta
PorRevisar del módulo, y el abogado lo mueve a Aprobados o Desaprobados según su
criterio. El sistema solo hace un análisis riguroso, verifica qué documentos hay y
cuáles faltan, y distribuye cada expediente a su carpeta.

Flujo por correo:
  1. Se acumulan los PDFs por message_id.
  2. Clasificador: identifica tipo, dependencia y los casos del correo.
  3. Procedencia (solo ESCALAFON con 2+ casos): inventaria cada PDF por separado y
     Python asigna los documentos por coincidencia exacta de cédula; cada analizador
     recibe SOLO los suyos.
  4. Analizador: una llamada por caso. Produce el análisis en HTML.
  5. Cada resultado sale con su carpeta destino (por tipo, sin veredicto).

Módulos activos: ESCALAFON e IVC.
"""

import os
import re
import time
import html
import json
import threading
import unicodedata
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
import openai
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

TZ_COLOMBIA = timezone(timedelta(hours=-5))

# ── Versión ────────────────────────────────────────────────────
BUILD_VERSION = "6.1"
BUILD_DATE    = "2026-09-09"
BUILD_FIX     = ("Nuevo modulo FONDO_PRESTACIONES con cinco subtipos (pension de jubilacion, "
                 "pension de invalidez, recurso de reposicion, seguro por muerte, auxilio por "
                 "muerte). Exento del filtrado por cedula porque sus soportes traen identificacion "
                 "de beneficiarios. Incluye tambien el fix de deduplicacion de casos IVC por NIT. "
                 "Modulos activos: ESCALAFON, IVC y FONDO_PRESTACIONES.")

# ── Configuración ──────────────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
API_SECRET     = os.environ.get("API_SECRET", "clave_secreta_make")
MODEL          = "gpt-5.4-mini-2026-03-17"
FORMATO_SALIDA = os.environ.get("FORMATO_SALIDA", "html").strip().lower()

MAX_INTENTOS_CLASIFICACION = int(os.environ.get("MAX_INTENTOS_CLASIFICACION", "3"))

# Modo de entrega de PDFs al analizador:
#   "filtrado"  → (por defecto) ESCALAFON con 2+ casos: procedencia previa asigna por
#                 cédula y cada caso recibe solo sus documentos.
#   "completo"  → cada caso recibe todos los PDFs (IVC y correos de un solo caso).
MODO_ENTREGA = os.environ.get("MODO_ENTREGA", "filtrado").strip().lower()

# Por encima de este número de PDFs se omite la procedencia previa (coste/tiempo).
LIMITE_PDFS_FILTRADO = int(os.environ.get("LIMITE_PDFS_FILTRADO", "16"))

client = openai.OpenAI(api_key=OPENAI_API_KEY)

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

# ── Acumulador de PDFs por correo ──────────────────────────────
pendientes = {}
lock_pendientes = threading.Lock()
TTL_SEGUNDOS = 300

# ── Mapa de tipo → prompt ──────────────────────────────────────
MAPA_PROMPTS = {
    "IVC":                "ivc",
    "ESCALAFON":          "escalafon",
    "FONDO_PRESTACIONES": "fondo_prestaciones",
    "OTRO":               "general",
}

# ── Mapa de tipo → carpeta destino (sin veredicto) ─────────────
MAPA_CARPETAS = {
    "IVC":                "IVC_POR_REVISAR",
    "ESCALAFON":          "ESCALAFON_POR_REVISAR",
    "FONDO_PRESTACIONES": "FONDO_PRESTACIONES_POR_REVISAR",
    "OTRO":               "ADVERTENCIA",
}

# Tipos cuyos soportes legítimos pueden llevar la identificación de un tercero.
# En IVC el sujeto es una institución con NIT, pero los soportes traen cédulas de
# representantes, rectores o propietarios. En FONDO_PRESTACIONES el sujeto es el
# docente causante, pero los soportes de los trámites por muerte traen cédulas de
# beneficiarios (cónyuge, hijos) o de quien sufragó los gastos fúnebres. En estos
# tipos NO se filtra por cédula.
TIPOS_CON_SOPORTES_DE_TERCEROS = {"IVC", "FONDO_PRESTACIONES"}


# ══════════════════════════════════════════════════════════════
# RENDERIZADO HTML
# ══════════════════════════════════════════════════════════════

ESTADOS_CLASE = {
    "coincide":                   "ok",
    "aportado":                   "ok",
    "cumple":                     "ok",
    "coincide_parcialmente":      "warn",
    "requiere_validacion_manual": "warn",
    "no_verificable":             "warn",
    "no_aplica":                  "neutral",
    "no_coincide":                "bad",
    "faltante":                   "bad",
    "inconsistente":              "bad",
}

# Concepto jurídico sugerido → (color, fondo, borde, etiqueta). Informativo, no veredicto.
CONCEPTO_ESTILO = {
    "expediente_completo":        ("#0f7b3d", "#e6f6ec", "#0f7b3d", "Expediente completo"),
    "viable_para_firma":          ("#0f7b3d", "#e6f6ec", "#0f7b3d", "Viable para firma"),
    "pendiente_por_soportes":     ("#8a5a00", "#fff5e0", "#8a5a00", "Pendiente por soportes"),
    "devolver_para_correccion":   ("#b3261e", "#fdecea", "#b3261e", "Devolver para corrección"),
    "requiere_validacion_manual": ("#8a5a00", "#fff5e0", "#8a5a00", "Requiere validación manual"),
    "sin_concepto":               ("#5b6b7b", "#eef1f5", "#5b6b7b", "Análisis para revisión"),
}

_RE_SEPARADOR_TABLA = re.compile(r'^\s*\|?[\s:|-]+\|?\s*$')
_RE_TITULO_NUM      = re.compile(r'^\s*(\d{1,2})[\.\)]\s+(.{2,120})$')
_RE_ETAPA           = re.compile(r'^\s*(ETAPA|SUBTIPO|MATRIZ)\b', re.IGNORECASE)
_RE_CONCEPTO = re.compile(
    r'(?:conclusion_juridica|concepto_juridico_sugerido|concepto_sugerido)\s*[:=]\s*'
    r'(expediente_completo|viable_para_firma|pendiente_por_soportes|'
    r'devolver_para_correccion|requiere_validacion_manual)',
    re.IGNORECASE
)


def extraer_concepto_sugerido(texto: str) -> str:
    """Lee el concepto jurídico sugerido. Informativo, no decide carpeta."""
    m = _RE_CONCEPTO.search(texto or "")
    return m.group(1).lower() if m else "sin_concepto"


def _inline(texto_plano: str) -> str:
    t = html.escape(texto_plano)
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    t = re.sub(r'(?<![\*\w])\*(?!\s)([^\*]+?)(?<!\s)\*(?![\*\w])', r'<em>\1</em>', t)
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
    return t


def _limpiar_marcas(texto: str) -> str:
    t = re.sub(r'__(.+?)__', r'\1', texto)
    t = re.sub(r'[\*`#]', '', t)
    return t.strip()


def _celda(texto_plano: str) -> str:
    crudo = _limpiar_marcas(texto_plano).strip()
    clave = crudo.lower().replace(" ", "_").replace("-", "_").strip(" .")
    clase = ESTADOS_CLASE.get(clave)
    if clase:
        return f'<td><span class="estado {clase}">{html.escape(crudo)}</span></td>'
    return f'<td>{_inline(texto_plano.strip())}</td>'


def _partir_fila(linea: str) -> list:
    return [c.strip() for c in linea.strip().strip("|").split("|")]


def _riesgo_clase(texto: str) -> str:
    t = texto.upper()
    if "ALTO" in t:
        return "bad"
    if "MEDIO" in t:
        return "warn"
    if "BAJO" in t:
        return "ok"
    return ""


def analisis_a_html_cuerpo(analisis_texto: str) -> str:
    lineas = analisis_texto.replace("\r\n", "\n").split("\n")
    salida = []
    i = 0
    n = len(lineas)

    while i < n:
        strip = lineas[i].strip()

        # La línea del concepto se muestra en el encabezado, no en el cuerpo.
        if _RE_CONCEPTO.search(_limpiar_marcas(strip)) and len(strip) < 90:
            i += 1
            continue

        if not strip:
            i += 1
            continue

        if re.fullmatch(r'[-=_═━]{3,}', strip):
            salida.append('<hr>')
            i += 1
            continue

        if strip.startswith("|") and strip.count("|") >= 2:
            filas = []
            while i < n and lineas[i].strip().startswith("|"):
                actual = lineas[i].strip()
                if not _RE_SEPARADOR_TABLA.match(actual):
                    filas.append(_partir_fila(actual))
                i += 1
            if filas:
                th = "".join(f'<th>{_inline(_limpiar_marcas(c))}</th>' for c in filas[0])
                trs = "".join(f'<tr>{"".join(_celda(c) for c in fila)}</tr>' for fila in filas[1:])
                salida.append(f'<div class="tabla-wrap"><table><thead><tr>{th}</tr></thead>'
                               f'<tbody>{trs}</tbody></table></div>')
            continue

        m_hash = re.match(r'^(#{1,6})\s+(.*)$', strip)
        if m_hash:
            texto = _limpiar_marcas(m_hash.group(2))
            nivel = "seccion" if len(m_hash.group(1)) <= 2 else "subseccion"
            salida.append(f'<h2 class="{nivel}">{html.escape(texto)}</h2>')
            i += 1
            continue

        m_num = _RE_TITULO_NUM.match(strip)
        if m_num:
            crudo = m_num.group(2).strip()
            resto = _limpiar_marcas(crudo)
            era_negrita = bool(re.fullmatch(r'\*\*.+\*\*|__.+__', crudo))
            if (len(resto) <= 90 and not resto.endswith((".", ":", ";"))
                    and any(ch.isalpha() for ch in resto)
                    and (resto.upper() == resto or era_negrita)):
                salida.append(f'<h2 class="seccion"><span class="num">{m_num.group(1)}</span>'
                               f'{html.escape(resto.upper())}</h2>')
                i += 1
                continue

        solo_texto = _limpiar_marcas(strip)
        if (solo_texto and len(solo_texto) <= 90 and solo_texto.upper() == solo_texto
                and any(ch.isalpha() for ch in solo_texto)
                and not solo_texto.startswith(("-", "•"))):
            etiqueta = "seccion" if _RE_ETAPA.match(solo_texto) else "subseccion"
            salida.append(f'<h3 class="{etiqueta}">{html.escape(solo_texto)}</h3>')
            i += 1
            continue

        if re.match(r'^[-•·*]\s+', strip):
            items = []
            while i < n and re.match(r'^\s*[-•·*]\s+', lineas[i]) and lineas[i].strip():
                contenido = re.sub(r'^\s*[-•·*]\s+', '', lineas[i]).strip()
                clase = _riesgo_clase(contenido[:30])
                marca = f' class="li-{clase}"' if clase else ""
                items.append(f'<li{marca}>{_inline(contenido)}</li>')
                i += 1
            salida.append(f'<ul>{"".join(items)}</ul>')
            continue

        salida.append(f'<p>{_inline(strip)}</p>')
        i += 1

    return "\n".join(salida)


def envolver_html(cuerpo_html: str, meta: dict) -> str:
    concepto = (meta.get("concepto_sugerido") or "sin_concepto").lower()
    color, fondo, borde, etiqueta = CONCEPTO_ESTILO.get(concepto, CONCEPTO_ESTILO["sin_concepto"])

    sujeto  = meta.get("sujeto") or "Expediente sin sujeto identificado"
    cedula  = meta.get("identificacion") or ""
    tipo    = meta.get("tipo") or ""
    subtipo = meta.get("subtipo") or ""
    asunto  = meta.get("asunto") or ""
    riesgo  = (meta.get("riesgo") or "").upper()
    fecha   = meta.get("fecha") or datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")

    chips = []
    if cedula:
        chips.append(f'<span class="chip"><b>{"NIT" if tipo == "IVC" else "C.C."}</b> '
                     f'{html.escape(str(cedula))}</span>')
    if tipo:
        chips.append(f'<span class="chip"><b>Módulo</b> {html.escape(tipo)}</span>')
    if subtipo:
        chips.append(f'<span class="chip"><b>Subtipo</b> {html.escape(str(subtipo))}</span>')
    if riesgo:
        chips.append(f'<span class="chip"><b>Riesgo</b> {html.escape(riesgo)}</span>')
    chips.append(f'<span class="chip"><b>Fecha</b> {html.escape(fecha)}</span>')
    chips_html = "".join(chips)

    asunto_html = f'<div class="asunto">{html.escape(asunto)}</div>' if asunto else ""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(sujeto)} — {html.escape(tipo)}</title>
<style>
  :root {{
    --azul:#12395c; --azul-claro:#2e6da4; --linea:#e2e8f0;
    --texto:#1f2933; --gris:#5b6b7b; --fondo:#eef1f5;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    font-family:'Segoe UI',system-ui,-apple-system,Roboto,Arial,sans-serif;
    background:var(--fondo); color:var(--texto);
    font-size:15px; line-height:1.65; padding:0 0 60px;
  }}
  header {{
    background:linear-gradient(135deg,var(--azul) 0%,#1c5480 100%);
    color:#fff; padding:26px 40px 22px; border-bottom:4px solid var(--azul-claro);
  }}
  .inner {{ max-width:940px; margin:0 auto; }}
  .entidad {{
    font-size:10.5px; letter-spacing:2.2px; text-transform:uppercase;
    opacity:.72; margin-bottom:8px; font-weight:600;
  }}
  header h1 {{ font-size:23px; font-weight:700; letter-spacing:.2px; }}
  .asunto {{ font-size:13.5px; opacity:.85; margin-top:5px; font-style:italic; }}
  .chips {{ margin-top:14px; display:flex; flex-wrap:wrap; gap:8px; }}
  .chip {{
    background:rgba(255,255,255,.13); border:1px solid rgba(255,255,255,.22);
    border-radius:20px; padding:4px 13px; font-size:12px;
  }}
  .chip b {{ font-weight:600; opacity:.75; margin-right:4px; }}
  .concepto {{
    display:inline-block; margin-top:16px; padding:9px 26px; border-radius:5px;
    font-size:14px; font-weight:700; letter-spacing:.6px;
    background:{fondo}; color:{color}; border:2px solid {borde};
  }}
  .nota-decision {{ margin-top:10px; font-size:11.5px; opacity:.78; font-style:italic; max-width:640px; }}
  main {{
    max-width:940px; margin:26px auto 0; padding:34px 40px;
    background:#fff; border-radius:8px; box-shadow:0 1px 3px rgba(16,36,60,.09);
  }}
  h2.seccion {{
    font-size:14.5px; font-weight:700; color:var(--azul);
    text-transform:uppercase; letter-spacing:.6px; border-left:4px solid var(--azul-claro);
    background:#eef4fa; padding:9px 14px; margin:30px 0 12px; border-radius:0 5px 5px 0;
    display:flex; align-items:center; gap:10px;
  }}
  h2.seccion:first-child {{ margin-top:0; }}
  .num {{
    background:var(--azul-claro); color:#fff; font-size:11px; width:21px; height:21px;
    border-radius:50%; display:inline-flex; align-items:center; justify-content:center; flex-shrink:0;
  }}
  h3.subseccion {{
    font-size:12.5px; font-weight:700; color:var(--gris);
    text-transform:uppercase; letter-spacing:.7px; margin:20px 0 7px;
  }}
  h3.seccion {{
    font-size:13.5px; font-weight:700; color:var(--azul);
    border-left:3px solid var(--azul-claro); background:#f3f7fb;
    padding:7px 12px; margin:24px 0 10px; border-radius:0 4px 4px 0;
    text-transform:uppercase; letter-spacing:.5px;
  }}
  p {{ margin:0 0 8px; }}
  ul {{ margin:6px 0 14px 4px; list-style:none; }}
  li {{ position:relative; padding-left:18px; margin-bottom:6px; }}
  li::before {{
    content:""; position:absolute; left:2px; top:.62em; width:6px; height:6px;
    border-radius:50%; background:var(--azul-claro);
  }}
  li.li-bad::before {{ background:#b3261e; }}
  li.li-warn::before {{ background:#c98a00; }}
  li.li-ok::before  {{ background:#0f7b3d; }}
  .tabla-wrap {{ overflow-x:auto; margin:12px 0 20px; border:1px solid var(--linea); border-radius:7px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  th {{
    background:var(--azul); color:#fff; text-align:left; padding:10px 14px;
    font-weight:600; font-size:12.5px; text-transform:uppercase; letter-spacing:.4px; white-space:nowrap;
  }}
  td {{ padding:9px 14px; border-top:1px solid var(--linea); vertical-align:top; }}
  tbody tr:nth-child(even) {{ background:#f7fafc; }}
  tbody tr:hover {{ background:#eef4fa; }}
  .estado {{
    display:inline-block; padding:3px 11px; border-radius:14px;
    font-size:11.5px; font-weight:600; white-space:nowrap;
  }}
  .estado.ok      {{ background:#e6f6ec; color:#0f7b3d; border:1px solid #b7e4c7; }}
  .estado.warn    {{ background:#fff5e0; color:#8a5a00; border:1px solid #f3d9a0; }}
  .estado.bad     {{ background:#fdecea; color:#b3261e; border:1px solid #f5c2bd; }}
  .estado.neutral {{ background:#eef1f5; color:#5b6b7b; border:1px solid #d5dce4; }}
  code {{ background:#eef1f5; padding:1px 6px; border-radius:4px; font-family:Consolas,Monaco,monospace; font-size:12.5px; }}
  hr {{ border:0; border-top:1px solid var(--linea); margin:22px 0; }}
  footer {{ max-width:940px; margin:18px auto 0; padding:0 40px; font-size:11px; color:#9aa5b1; text-align:center; }}
  @media print {{
    body {{ background:#fff; }}
    main {{ box-shadow:none; padding:0; }}
    header {{ background:var(--azul) !important; -webkit-print-color-adjust:exact; }}
  }}
  @media (max-width:640px) {{
    header, main {{ padding-left:18px; padding-right:18px; }}
    main {{ border-radius:0; }}
  }}
</style>
</head>
<body>
<header>
  <div class="inner">
    <div class="entidad">Secretaría Distrital de Educación de Barranquilla · Revisión jurídica automatizada</div>
    <h1>{html.escape(sujeto)}</h1>
    {asunto_html}
    <div class="chips">{chips_html}</div>
    <div class="concepto">CONCEPTO SUGERIDO: {html.escape(etiqueta)}</div>
    <div class="nota-decision">
      Este es un análisis preventivo. La decisión final de aprobación o desaprobación
      corresponde al abogado revisor.
    </div>
  </div>
</header>
<main>
{cuerpo_html}
</main>
<footer>
  Documento generado automáticamente por juridico-service v{BUILD_VERSION} · {html.escape(fecha)}<br>
  Este análisis es una revisión preliminar y no sustituye el criterio del abogado revisor.
</footer>
</body>
</html>"""


def renderizar_analisis(analisis_texto: str, meta: dict) -> str:
    if FORMATO_SALIDA != "html":
        return analisis_texto
    try:
        return envolver_html(analisis_a_html_cuerpo(analisis_texto), meta)
    except Exception as e:
        print(f"[WARN] Falló el render HTML, se devuelve texto plano: {e}")
        return analisis_texto


# ══════════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════════

_RE_DIGITOS = re.compile(r'\d{5,}')


def _normalizar_cedula(texto):
    if not texto:
        return None
    m = _RE_DIGITOS.search(str(texto).replace(".", "").replace(" ", ""))
    return m.group(0) if m else None


def limpiar_texto(texto: str) -> str:
    if not texto:
        return ""
    texto = unicodedata.normalize('NFD', texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != 'Mn')
    texto = re.sub(r'[<>:"/\\|?*]', '', texto)
    texto = re.sub(r'\s+', ' ', texto)
    return texto.strip()


def cargar_prompt(nombre: str) -> str:
    ruta = os.path.join(PROMPTS_DIR, f"{nombre}.txt")
    if not os.path.exists(ruta):
        if nombre in {"procedencia", "clasificador"}:
            raise FileNotFoundError(f"Falta el prompt obligatorio '{nombre}.txt' en {PROMPTS_DIR}.")
        ruta = os.path.join(PROMPTS_DIR, "general.txt")
    with open(ruta, "r", encoding="utf-8") as f:
        return f.read()


def construir_content(file_ids: list, texto_prompt: str) -> list:
    content = [{"type": "file", "file": {"file_id": fid}} for fid in file_ids]
    content.append({"type": "text", "text": texto_prompt})
    return content


def construir_nombre_archivo(caso: dict, tipo: str, message_id: str) -> str:
    """
    SUJETO - IDENTIFICACION - TIPO [ - SUBTIPO] - YYYY-MM-DD
    El subtipo se incluye cuando existe (en IVC ocho trámites comparten carpeta).
    """
    fecha = datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")
    sujeto = limpiar_texto(caso.get("sujeto") or "")
    identificacion = limpiar_texto(caso.get("identificacion") or "")

    subtipo = ""
    subtipo_raw = (caso.get("subtipo") or "").strip()
    if subtipo_raw:
        s = limpiar_texto(subtipo_raw).upper()
        s = re.sub(r'^(IVC|ESCALAFON|FONDO|PLANTA)[_\s-]*', '', s)
        subtipo = s.replace("_", "-").strip("- ")

    if sujeto and identificacion:
        partes = [sujeto, identificacion, tipo]
    elif sujeto:
        partes = [sujeto, tipo]
    else:
        asunto = limpiar_texto(caso.get("asunto") or "Sin asunto")[:60]
        sufijo = message_id[-8:] if message_id else "sinid"
        partes = [asunto, tipo]
        if subtipo:
            partes.append(subtipo)
        partes += [fecha, sufijo]
        return " - ".join(partes)[:180]

    if subtipo:
        partes.append(subtipo)
    partes.append(fecha)
    return " - ".join(partes)[:180]


# ══════════════════════════════════════════════════════════════
# OPENAI
# ══════════════════════════════════════════════════════════════

def subir_pdf(pdf_bytes: bytes, nombre: str) -> str:
    return client.files.create(
        file=(nombre, pdf_bytes, "application/pdf"), purpose="user_data"
    ).id


def esperar_procesamiento(file_id: str, intentos: int = 15) -> bool:
    for _ in range(intentos):
        if client.files.retrieve(file_id).status == "processed":
            return True
        time.sleep(2)
    return False


def limpiar_archivos(file_ids: list):
    for fid in file_ids:
        try:
            client.files.delete(fid)
        except Exception:
            pass


def _completar(prompt_content) -> str:
    response = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": prompt_content}]
    )
    return response.choices[0].message.content


def _parsear_json(texto: str):
    if "```" in texto:
        for p in texto.split("```"):
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            try:
                return json.loads(p)
            except Exception:
                continue
    return json.loads(texto)


# ── Clasificador ───────────────────────────────────────────────

def validar_clasificacion(clasificacion: dict) -> list:
    errores = []
    casos = clasificacion.get("casos", []) or []
    if not casos:
        return ["No devolviste ningun caso. Debe haber al menos uno."]

    vistos = set()
    for k, caso in enumerate(casos, start=1):
        sujeto = (caso.get("sujeto") or "").strip().upper()
        ident  = str(caso.get("identificacion") or "").strip()
        if not sujeto:
            errores.append(f"El caso numero {k} no tiene 'sujeto'.")
            continue
        if (sujeto, ident) in vistos:
            errores.append(f"El sujeto '{sujeto}' aparece en mas de un caso.")
        vistos.add((sujeto, ident))

    if clasificacion.get("cantidad_casos") not in (None, len(casos)):
        errores.append(f"cantidad_casos no coincide con el numero de casos ({len(casos)}).")
    return errores


def llamada_clasificador(file_ids: list, errores_previos: list = None) -> dict:
    prompt = cargar_prompt("clasificador") + (
        "\n\n===============================================================\n"
        "NO REPARTAS DOCUMENTOS ENTRE LOS CASOS\n"
        "===============================================================\n"
        "Identifica el tipo, la dependencia y cuantas PERSONAS o INSTITUCIONES distintas "
        "tienen un acto administrativo principal. Para cada una: sujeto, identificacion, "
        "asunto, subtipo, riesgo y urgencia. Los titulos y certificados NO generan casos "
        "por si solos. Puedes omitir 'indices_documentos' y 'documentos'.\n"
    )
    if errores_previos:
        prompt += ("\n\nCORRIGE TU INTENTO ANTERIOR:\n"
                   + "\n".join(f"- {e}" for e in errores_previos) + "\n")

    texto = _completar(construir_content(file_ids, prompt)).strip()
    try:
        return _parsear_json(texto)
    except Exception:
        print(f"[WARN] No se pudo parsear la clasificación: {texto[:400]}")
        return {"tipo": "OTRO", "dependencia": "DESCONOCIDO", "cantidad_casos": 1,
                "casos": [{"sujeto": None, "identificacion": None, "asunto": "No identificado",
                           "riesgo": "MEDIO", "urgente": False, "subtipo": None}]}


# ── Procedencia (asignación por cédula) ────────────────────────

def _procedencia_de_un_pdf(file_id: str, indice: int) -> dict:
    prompt = cargar_prompt("procedencia")
    base = {"indice": indice, "documento": "documento no identificado", "titular": None,
            "cedula": None, "dato_clave": "no identificado", "legible": False}
    try:
        datos = _parsear_json(_completar(construir_content([file_id], prompt)).strip())
        cedula = _normalizar_cedula(datos.get("cedula"))
        base.update({
            "documento":  datos.get("documento") or base["documento"],
            "titular":    datos.get("titular"),
            "cedula":     cedula,
            "dato_clave": datos.get("dato_clave") or base["dato_clave"],
            "legible":    bool(datos.get("legible")) and bool(cedula),
        })
    except Exception as e:
        print(f"  [WARN] No se pudo inventariar el PDF #{indice}: {e}")
    return base


def inventariar_procedencia(file_ids: list) -> list:
    resultados = [None] * len(file_ids)
    hilos = []

    def trabajo(idx, fid):
        resultados[idx] = _procedencia_de_un_pdf(fid, idx)

    for idx, fid in enumerate(file_ids):
        t = threading.Thread(target=trabajo, args=(idx, fid), daemon=True)
        t.start()
        hilos.append(t)
    for t in hilos:
        t.join(timeout=180)

    for idx in range(len(file_ids)):
        if resultados[idx] is None:
            resultados[idx] = {"indice": idx, "documento": "documento no inventariado",
                               "titular": None, "cedula": None,
                               "dato_clave": "no identificado", "legible": False}
    return resultados


def asignar_por_cedula(inventario: list, casos: list, total_pdfs: int) -> tuple:
    cedula_a_caso = {}
    for c in casos:
        ced = _normalizar_cedula(c.get("identificacion"))
        if ced:
            cedula_a_caso[ced] = (c.get("sujeto"), c.get("identificacion"))

    asignacion = {(c.get("sujeto"), c.get("identificacion")): [] for c in casos}
    no_asignados = []
    vistos = set()

    for d in inventario:
        idx = d.get("indice")
        if not isinstance(idx, int) or not (0 <= idx < total_pdfs) or idx in vistos:
            continue
        vistos.add(idx)
        ced = _normalizar_cedula(d.get("cedula"))
        if not d.get("legible") or not ced:
            no_asignados.append({"indice": idx, "documento": d.get("documento"),
                                 "razon": "No se pudo leer la cedula del documento."})
        elif ced not in cedula_a_caso:
            no_asignados.append({"indice": idx, "documento": d.get("documento"),
                                 "razon": f"La cedula {ced} no corresponde a ningun expediente del correo."})
        else:
            asignacion[cedula_a_caso[ced]].append(idx)

    for idx in sorted(set(range(total_pdfs)) - vistos):
        no_asignados.append({"indice": idx, "documento": "documento no inventariado",
                             "razon": "El inventario no incluyo este PDF."})

    for k in asignacion:
        asignacion[k] = sorted(asignacion[k])
    return asignacion, no_asignados


# ── Analizador ─────────────────────────────────────────────────

def llamada_analizador(file_ids_caso: list, caso: dict, tipo_general: str,
                       dependencia: str, filtrado: bool) -> str:
    prompt = cargar_prompt(MAPA_PROMPTS.get(tipo_general, "general"))

    sujeto = caso.get("sujeto") or "este expediente"
    ident  = caso.get("identificacion") or "sin identificacion"
    subtipo = caso.get("subtipo")
    subtipo_linea = f"Subtipo detectado por el clasificador: {subtipo}\n" if subtipo else ""

    if filtrado:
        bloque = (
            f"[EXPEDIENTE YA FILTRADO]\n"
            f"El titular de este expediente es: {sujeto}, identificacion {ident}.\n"
            f"Los PDFs que recibes fueron seleccionados comparando la cedula leida en cada "
            f"documento con la de este expediente, asi que en principio todos le pertenecen. "
            f"Aun asi, confirma en la matriz de procedencia el nombre y la cedula de cada uno. "
            f"Si falta un soporte, repórtalo como faltante con normalidad.\n\n"
        )
    else:
        bloque = (
            f"[EXPEDIENTE]\n"
            f"El titular de este expediente es: {sujeto}, identificacion {ident}.\n"
            f"Verifica el titular de cada documento y analiza solo lo que pertenezca a este "
            f"expediente.\n\n"
        )

    contexto = (
        f"[CONTEXTO DE CLASIFICACION]\n"
        f"Tipo: {tipo_general}\n"
        f"Dependencia: {dependencia}\n"
        f"{subtipo_linea}"
        f"Asunto: {caso.get('asunto', 'N/A')}\n"
        f"Sujeto: {sujeto}\n"
        f"Identificacion: {ident}\n\n"
        f"{bloque}"
    )
    return _completar(construir_content(file_ids_caso, contexto + prompt))


# ── Advertencia ────────────────────────────────────────────────

def construir_advertencia(huerfanos: list, message_id: str) -> dict:
    fecha = datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")
    contenido = ("DOCUMENTOS NO ASOCIADOS A NINGUN EXPEDIENTE\n\n"
                 f"Correo de origen: {message_id}\n"
                 f"Fecha de procesamiento: {fecha}\n\n"
                 f"Se detectaron {len(huerfanos)} documento(s) sin asociar.\n\n"
                 "DETALLE\n\n")
    for h in huerfanos:
        contenido += f"- {h.get('nombre', h.get('documento', 'Documento'))} — {h.get('razon', 'Sin razon')}\n"
    contenido += ("\nACCION\n\n"
                  "- Revisar el correo original y verificar que correspondan a un caso.\n"
                  "- Reenviar el expediente si faltan documentos principales.\n")

    meta = {"sujeto": "Advertencia del sistema", "tipo": "ADVERTENCIA",
            "asunto": "Documentos sin asociar", "concepto_sugerido": "sin_concepto", "fecha": fecha}
    return {
        "tipo": "ADVERTENCIA", "carpeta": "ADVERTENCIA",
        "nombre_archivo": f"ADVERTENCIA - {message_id[-8:]} - {fecha}",
        "sujeto": None, "identificacion": None, "subtipo": None,
        "concepto_sugerido": "sin_concepto",
        "analisis": renderizar_analisis(contenido, meta),
        "analisis_texto": contenido, "message_id": message_id,
    }


# ══════════════════════════════════════════════════════════════
# PROCESAMIENTO
# ══════════════════════════════════════════════════════════════

def limpiar_pendientes_vencidos():
    ahora = time.time()
    with lock_pendientes:
        for mid in [m for m, d in pendientes.items() if ahora - d["timestamp"] > TTL_SEGUNDOS]:
            print(f"[WARN] Descartando correo vencido: {mid}")
            del pendientes[mid]


def procesar_correo(message_id: str, archivos_datos: list) -> dict:
    file_ids = []
    try:
        for archivo in archivos_datos:
            print(f"Subiendo {archivo['nombre']}...")
            file_ids.append(subir_pdf(archivo["bytes"], archivo["nombre"]))

        print("Esperando procesamiento...")
        for fid in file_ids:
            if not esperar_procesamiento(fid):
                raise Exception(f"Timeout esperando procesamiento de {fid}")

        total_pdfs = len(file_ids)

        # 1) CLASIFICAR
        print("Clasificando...")
        clasificacion, errores = None, []
        for intento in range(1, MAX_INTENTOS_CLASIFICACION + 1):
            clasificacion = llamada_clasificador(file_ids, errores_previos=errores)
            errores = validar_clasificacion(clasificacion)
            if not errores:
                break
            print(f"  [WARN] Intento {intento}: {errores}")

        tipo_general = clasificacion.get("tipo", "OTRO").strip().upper()
        dependencia  = (clasificacion.get("dependencia") or "DESCONOCIDO").strip().upper()
        casos = [c for c in clasificacion.get("casos", []) if (c.get("sujeto") or "").strip()]
        huerfanos = list(clasificacion.get("documentos_huerfanos", []) or [])

        unicos = {}
        for c in casos:
            unicos.setdefault((c.get("sujeto"), c.get("identificacion")), c)
        casos = list(unicos.values())

        # En IVC un mismo NIT = un mismo establecimiento educativo. Si el clasificador
        # devuelve varios casos con el mismo NIT (por ejemplo, un caso para la persona
        # juridica compradora y otro para el establecimiento en un cambio de titular),
        # los fusionamos en uno y preferimos el sujeto que no parezca una persona
        # juridica (S.A.S., LTDA, FUNDACION, ...) como nombre del establecimiento.
        if tipo_general == "IVC":
            _MARCAS_JURIDICAS = (" SAS", " S.A.S", " S.A.S.", " LTDA", " LTDA.",
                                 "FUNDACION", "FUNDACIoN", "ASOCIACION", "CORPORACION",
                                 " S.A", " S.A.")
            def _parece_persona_juridica(nombre: str) -> bool:
                n = (nombre or "").upper()
                return any(m in n for m in _MARCAS_JURIDICAS)

            por_nit = {}
            for c in casos:
                nit = (c.get("identificacion") or "").strip()
                if not nit:
                    por_nit.setdefault(id(c), c)
                    continue
                if nit not in por_nit:
                    por_nit[nit] = c
                else:
                    prev = por_nit[nit]
                    if _parece_persona_juridica(prev.get("sujeto")) and \
                       not _parece_persona_juridica(c.get("sujeto")):
                        por_nit[nit] = c
                    print(f"  [WARN] IVC: dos casos con NIT {nit} "
                          f"('{prev.get('sujeto')}' vs '{c.get('sujeto')}') "
                          f"fusionados en uno.")
            casos = list(por_nit.values())

        print(f"Tipo: {tipo_general} | Casos: {len(casos)}")

        # 2) ¿SE FILTRA POR CÉDULA?
        filtrar = (
            MODO_ENTREGA == "filtrado"
            and tipo_general not in TIPOS_CON_SOPORTES_DE_TERCEROS
            and len(casos) >= 2
            and total_pdfs <= LIMITE_PDFS_FILTRADO
        )

        asignacion = {}
        if filtrar:
            print(f"Inventariando procedencia de {total_pdfs} PDFs (uno por llamada)...")
            inventario = inventariar_procedencia(file_ids)
            for d in inventario:
                print(f"  PDF #{d['indice']}: {d.get('documento')} | "
                      f"{d.get('titular') or 'sin titular'} | "
                      f"{d.get('cedula') if d.get('legible') else 'ILEGIBLE'}")
            asignacion, no_asignados = asignar_por_cedula(inventario, casos, total_pdfs)
            for c in casos:
                clave = (c.get("sujeto"), c.get("identificacion"))
                print(f"  {c.get('sujeto')}: {len(asignacion.get(clave, []))} documento(s)")
            huerfanos += [{"nombre": f"{d['documento']} (PDF #{d['indice']+1})", "razon": d["razon"]}
                          for d in no_asignados]

        # 3) ANALIZAR
        resultados = []
        fecha_hoy = datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")

        for i, caso in enumerate(casos, start=1):
            sujeto = caso.get("sujeto", "sin_nombre")
            print(f"[{i}/{len(casos)}] Analizando: {sujeto}")

            if filtrar:
                indices = asignacion.get((caso.get("sujeto"), caso.get("identificacion")), [])
                file_ids_caso = [file_ids[k] for k in indices]
                print(f"  Recibe {len(file_ids_caso)} PDF(s): {indices}")
            else:
                file_ids_caso = file_ids
                print(f"  Recibe el expediente completo: {len(file_ids_caso)} PDF(s)")

            if not file_ids_caso:
                print(f"  [WARN] Sin documentos asignados. Se genera archivo de revision manual.")
                texto = (
                    f"1. RESUMEN DEL EXPEDIENTE\n\n"
                    f"Expediente de {sujeto}, identificacion {caso.get('identificacion') or 'no indicada'}.\n\n"
                    "NO FUE POSIBLE ASOCIAR DOCUMENTOS A ESTE EXPEDIENTE\n\n"
                    "El sistema identifico el caso pero no pudo asociar con certeza ninguno de los "
                    "PDFs. Ocurre cuando no se logra leer la cedula, o cuando no coincide con la del "
                    "expediente. Por seguridad, el sistema prefiere no asignar antes que asignar por "
                    "suposicion.\n\n"
                    "2. ACCION REQUERIDA\n\n"
                    "- Revisar manualmente los documentos del correo original.\n"
                    "- Verificar que los soportes de este expediente esten adjuntos.\n"
                    "- Revisar el archivo de ADVERTENCIA de este correo.\n\n"
                    "conclusion_juridica: requiere_validacion_manual"
                )
                concepto = "requiere_validacion_manual"
            else:
                texto = llamada_analizador(file_ids_caso, caso, tipo_general, dependencia, filtrar)
                concepto = extraer_concepto_sugerido(texto)

            carpeta = MAPA_CARPETAS.get(tipo_general, "ADVERTENCIA")
            print(f"  Concepto: {concepto} | Carpeta: {carpeta}")

            meta = {
                "sujeto": caso.get("sujeto"), "identificacion": caso.get("identificacion"),
                "tipo": tipo_general, "subtipo": caso.get("subtipo"),
                "asunto": (caso.get("asunto") or "").strip(),
                "riesgo": (caso.get("riesgo") or "MEDIO").strip().upper(),
                "concepto_sugerido": concepto, "fecha": fecha_hoy,
            }
            resultados.append({
                "tipo": tipo_general, "dependencia": dependencia,
                "subtipo": caso.get("subtipo"), "asunto": (caso.get("asunto") or "").strip(),
                "sujeto": caso.get("sujeto"), "identificacion": caso.get("identificacion"),
                "radicado": caso.get("radicado"), "vencimiento": caso.get("vencimiento"),
                "riesgo": (caso.get("riesgo") or "MEDIO").strip().upper(),
                "urgente": caso.get("urgente", False),
                "concepto_sugerido": concepto, "carpeta": carpeta,
                "nombre_archivo": construir_nombre_archivo(caso, tipo_general, message_id),
                "analisis": renderizar_analisis(texto, meta), "analisis_texto": texto,
                "message_id": message_id,
            })

        if huerfanos:
            print(f"[!] {len(huerfanos)} documento(s) sin asociar → advertencia")
            resultados.append(construir_advertencia(huerfanos, message_id))

        return {
            "message_id": message_id, "tipo_general": tipo_general,
            "cantidad_casos": len(casos), "cantidad_huerfanos": len(huerfanos),
            "archivos_procesados": total_pdfs, "formato_salida": FORMATO_SALIDA,
            "modo_entrega": "filtrado" if filtrar else "completo",
            "resultados": resultados,
        }
    finally:
        limpiar_archivos(file_ids)


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "version": BUILD_VERSION, "build_date": BUILD_DATE, "fix": BUILD_FIX,
        "model": MODEL, "formato_salida": FORMATO_SALIDA, "modo_entrega": MODO_ENTREGA,
        "limite_pdfs_filtrado": LIMITE_PDFS_FILTRADO,
        "modulos_activos": ["ESCALAFON", "IVC", "FONDO_PRESTACIONES"], "status": "ok",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": BUILD_VERSION})


@app.route("/preview", methods=["GET", "POST"])
def preview():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        return renderizar_analisis(data.get("analisis", ""), data.get("meta", {})), 200, \
            {"Content-Type": "text/html; charset=utf-8"}

    ejemplo = """1. RESUMEN DEL EXPEDIENTE

Institucion: COLEGIO DE PRUEBA
NIT: 900000000
Tramite detectado: cierre del establecimiento educativo

2. SUBTIPO IDENTIFICADO

ivc_cierre — se identifica por la solicitud de cierre y el acta de aviso a la comunidad.

3. MATRIZ DE DOCUMENTOS REQUERIDOS

| Documento requerido | Caracter | Estado | Observacion |
|---|---|---|---|
| Acta de aviso a la comunidad (6 meses antes) | Obligatorio | aportado | Cumple la antelacion |
| Registros de evaluacion y promocion | Obligatorio | aportado | Foliados y firmados |
| Acta del consejo directivo con alumnos | Obligatorio | faltante | No se aporto |
| Fecha de cierre y mecanismos de culminacion | Obligatorio | aportado | Indicada en la solicitud |

4. RIESGOS DETECTADOS

- ALTO: no se detectaron riesgos de nivel alto.
- MEDIO: falta el acta del consejo directivo con la relacion de alumnos.
- BAJO: la documentacion aportada es coherente entre si.

5. NOTA PARA EL ABOGADO REVISOR

El expediente esta casi completo. El punto que requiere su criterio es la ausencia del acta del
consejo directivo con la relacion de alumnos, necesaria para expedir los certificados.

conclusion_juridica: pendiente_por_soportes"""

    meta = {"sujeto": "COLEGIO DE PRUEBA", "identificacion": "900000000", "tipo": "IVC",
            "subtipo": "ivc_cierre", "asunto": "Vista previa del formato — datos ficticios",
            "riesgo": "MEDIO", "concepto_sugerido": "pendiente_por_soportes",
            "fecha": datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")}
    return envolver_html(analisis_a_html_cuerpo(ejemplo), meta), 200, \
        {"Content-Type": "text/html; charset=utf-8"}


@app.route("/analizar", methods=["POST"])
def analizar():
    if request.headers.get("X-API-Secret") != API_SECRET:
        return jsonify({"error": "No autorizado"}), 401

    archivos = request.files.getlist("pdf")
    if not archivos:
        return jsonify({"error": "No se recibieron archivos PDF"}), 400

    message_id  = request.form.get("message_id", "sin_id")
    total_files = int(request.form.get("total_files", 1))

    limpiar_pendientes_vencidos()

    with lock_pendientes:
        if message_id not in pendientes:
            pendientes[message_id] = {"archivos": [], "timestamp": time.time()}
        for archivo in archivos:
            pendientes[message_id]["archivos"].append(
                {"bytes": archivo.read(), "nombre": archivo.filename or "documento.pdf"}
            )
        recibidos = len(pendientes[message_id]["archivos"])

    print(f"[{message_id}] Recibidos {recibidos}/{total_files}")

    if recibidos < total_files:
        return jsonify({"status": "acumulando", "recibidos": recibidos,
                        "esperados": total_files, "message_id": message_id}), 202

    with lock_pendientes:
        datos = pendientes.pop(message_id)["archivos"]

    try:
        return jsonify(procesar_correo(message_id, datos)), 200
    except Exception as e:
        print(f"Error procesando {message_id}: {e}")
        return jsonify({"error": str(e), "message_id": message_id}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)