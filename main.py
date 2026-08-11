"""
MICROSERVICIO JURÍDICO v3.8
Arquitectura multi-caso: un correo puede contener varios casos del mismo tipo.
Flujo: Clasificador identifica N casos → Analizador se ejecuta N veces → Devuelve resultados[].
NUEVO v3.8: el campo "analisis" se entrega como HTML formateado listo para Dropbox.
"""

import os
import re
import time
import html
import threading
import json
import unicodedata
from datetime import datetime, timezone, timedelta

# Zona horaria de Colombia (UTC-5)
TZ_COLOMBIA = timezone(timedelta(hours=-5))
from flask import Flask, request, jsonify
import openai
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Versión del build ──────────────────────────────────────────
BUILD_VERSION = "3.8"
BUILD_DATE    = "2026-08-11"
BUILD_FIX     = "Salida HTML formateada: el analisis se entrega maquetado (encabezado, badge de veredicto, tablas y estados con color) para guardarse como .html en Dropbox"

# ── Configuración ──────────────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
API_SECRET     = os.environ.get("API_SECRET", "clave_secreta_make")
MODEL          = "gpt-5.4-mini-2026-03-17"

# Formato de salida del campo "analisis": "html" o "texto"
FORMATO_SALIDA = os.environ.get("FORMATO_SALIDA", "html").strip().lower()

client = openai.OpenAI(api_key=OPENAI_API_KEY)

# ── Acumulador de PDFs por correo ──────────────────────────────
pendientes = {}
lock_pendientes = threading.Lock()
TTL_SEGUNDOS = 300

# ── Mapa de tipos a archivos de prompt ────────────────────────
MAPA_PROMPTS = {
    "RESOLUCION":     "resolucion",
    "RETIRO_FORZOSO": "retiro_forzoso",
    "CESANTIAS":      "cesantias",
    "IVC":            "ivc",
    "ESCALAFON":      "escalafon",
    "TUTELA":         "tutela",
    "PETICION":       "peticion",
    "REQUERIMIENTO":  "requerimiento",
    "OFICIO":         "oficio",
    "OTRO":           "general",
}

# ── Mapa tipo+veredicto → carpeta destino ─────────────────────
MAPA_CARPETAS = {
    ("RESOLUCION",     "APROBADO"):    "RESOLUCION_APROBADO",
    ("RESOLUCION",     "DESAPROBADO"): "RESOLUCION_DESAPROBADO",
    ("RETIRO_FORZOSO", "APROBADO"):    "RETIRO_FORZOSO_APROBADO",
    ("RETIRO_FORZOSO", "DESAPROBADO"): "RETIRO_FORZOSO_DESAPROBADO",
    ("CESANTIAS",      "APROBADO"):    "CESANTIAS_APROBADO",
    ("CESANTIAS",      "DESAPROBADO"): "CESANTIAS_DESAPROBADO",
    ("IVC",            "APROBADO"):    "IVC_APROBADO",
    ("IVC",            "DESAPROBADO"): "IVC_DESAPROBADO",
    ("ESCALAFON",      "APROBADO"):    "ESCALAFON_APROBADO",
    ("ESCALAFON",      "DESAPROBADO"): "ESCALAFON_DESAPROBADO",
    ("TUTELA",         "APROBADO"):    "TUTELA_APROBADO",
    ("TUTELA",         "DESAPROBADO"): "TUTELA_DESAPROBADO",
    ("PETICION",       "APROBADO"):    "PETICION_APROBADO",
    ("PETICION",       "DESAPROBADO"): "PETICION_DESAPROBADO",
    ("REQUERIMIENTO",  "APROBADO"):    "REQUERIMIENTO_APROBADO",
    ("REQUERIMIENTO",  "DESAPROBADO"): "REQUERIMIENTO_DESAPROBADO",
}

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


# ══════════════════════════════════════════════════════════════
# RENDERIZADO HTML DEL ANÁLISIS
# ══════════════════════════════════════════════════════════════

# Estados de comparación → clase CSS (ver hoja de estilos)
ESTADOS_CLASE = {
    "coincide":                       "ok",
    "aportado":                       "ok",
    "cumple":                         "ok",
    "coincide_parcialmente":          "warn",
    "coincide_con_validacion_manual": "warn",
    "requiere_validacion_manual":     "warn",
    "no_verificable":                 "warn",
    "no_aplica":                      "neutral",
    "no_coincide":                    "bad",
    "faltante":                       "bad",
    "inconsistente":                  "bad",
}

# Colores del badge de veredicto
VEREDICTO_ESTILO = {
    "APROBADO":          ("#0f7b3d", "#e6f6ec", "#0f7b3d"),
    "DESAPROBADO":       ("#b3261e", "#fdecea", "#b3261e"),
    "REQUIERE_REVISION": ("#8a5a00", "#fff5e0", "#8a5a00"),
    "ADVERTENCIA":       ("#8a5a00", "#fff5e0", "#8a5a00"),
}

_RE_SEPARADOR_TABLA = re.compile(r'^\s*\|?[\s:|-]+\|?\s*$')
_RE_TITULO_NUM      = re.compile(r'^\s*(\d{1,2})[\.\)]\s+(.{2,120})$')
_RE_ETAPA           = re.compile(r'^\s*(ETAPA|SUBTIPO ACTIVO|MATRIZ)\b', re.IGNORECASE)


def _inline(texto_plano: str) -> str:
    """Escapa el texto y convierte marcas inline de markdown a HTML."""
    t = html.escape(texto_plano)
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    t = re.sub(r'__(.+?)__', r'<strong>\1</strong>', t)
    t = re.sub(r'(?<![\*\w])\*(?!\s)([^\*]+?)(?<!\s)\*(?![\*\w])', r'<em>\1</em>', t)
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
    return t


def _limpiar_marcas(texto: str) -> str:
    """
    Quita marcas markdown conservando los guiones bajos internos.
    Importante: los estados del sistema usan snake_case (no_aplica,
    requiere_validacion_manual), así que solo se elimina el subrayado
    cuando viene en pareja como marca de negrita (__texto__).
    """
    t = re.sub(r'__(.+?)__', r'\1', texto)   # negrita con doble guion bajo
    t = re.sub(r'[\*`#]', '', t)             # asteriscos, comillas y almohadillas
    return t.strip()


def _celda(texto_plano: str) -> str:
    """Renderiza una celda; si su contenido es un estado conocido, lo pinta."""
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
    """Convierte el texto del análisis (markdown ligero) en HTML estructurado."""
    lineas = analisis_texto.replace("\r\n", "\n").split("\n")
    salida = []
    i = 0
    n = len(lineas)

    while i < n:
        linea = lineas[i]
        strip = linea.strip()

        # 1. El veredicto se muestra en el encabezado, no en el cuerpo
        if strip.upper().startswith("VEREDICTO:"):
            i += 1
            continue

        # 2. Línea vacía
        if not strip:
            i += 1
            continue

        # 3. Separadores decorativos (---, ===, ═══)
        if re.fullmatch(r'[-=_═━]{3,}', strip):
            salida.append('<hr>')
            i += 1
            continue

        # 4. Bloque de tabla markdown
        if strip.startswith("|") and strip.count("|") >= 2:
            filas = []
            while i < n and lineas[i].strip().startswith("|"):
                actual = lineas[i].strip()
                if not _RE_SEPARADOR_TABLA.match(actual):
                    filas.append(_partir_fila(actual))
                i += 1

            if filas:
                encabezado = filas[0]
                cuerpo_filas = filas[1:]
                th = "".join(
                    f'<th>{_inline(_limpiar_marcas(c))}</th>' for c in encabezado
                )
                trs = []
                for fila in cuerpo_filas:
                    tds = "".join(_celda(c) for c in fila)
                    trs.append(f'<tr>{tds}</tr>')
                salida.append(
                    '<div class="tabla-wrap"><table>'
                    f'<thead><tr>{th}</tr></thead>'
                    f'<tbody>{"".join(trs)}</tbody>'
                    '</table></div>'
                )
            continue

        # 5. Encabezados markdown (#, ##, ###)
        m_hash = re.match(r'^(#{1,6})\s+(.*)$', strip)
        if m_hash:
            texto = _limpiar_marcas(m_hash.group(2))
            nivel = "seccion" if len(m_hash.group(1)) <= 2 else "subseccion"
            salida.append(f'<h2 class="{nivel}">{html.escape(texto)}</h2>')
            i += 1
            continue

        # 6. Sección numerada: "1. RESUMEN DEL CASO"
        m_num = _RE_TITULO_NUM.match(strip)
        if m_num:
            resto = _limpiar_marcas(m_num.group(2))
            # Solo es título si es corto y va en mayúsculas
            es_titulo = (
                len(resto) <= 90
                and resto.upper() == resto
                and not resto.endswith((".", ":"))
                and any(ch.isalpha() for ch in resto)
            )
            if es_titulo:
                salida.append(
                    f'<h2 class="seccion">'
                    f'<span class="num">{m_num.group(1)}</span>'
                    f'{html.escape(resto)}</h2>'
                )
                i += 1
                continue

        # 7. Línea completamente en mayúsculas → subtítulo
        solo_texto = _limpiar_marcas(strip)
        if (solo_texto
                and len(solo_texto) <= 90
                and solo_texto.upper() == solo_texto
                and any(ch.isalpha() for ch in solo_texto)
                and not solo_texto.startswith(("-", "•"))):
            etiqueta = "seccion" if _RE_ETAPA.match(solo_texto) else "subseccion"
            salida.append(f'<h3 class="{etiqueta}">{html.escape(solo_texto)}</h3>')
            i += 1
            continue

        # 8. Lista de viñetas (agrupada)
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

        # 9. Párrafo normal
        salida.append(f'<p>{_inline(strip)}</p>')
        i += 1

    return "\n".join(salida)


def envolver_html(cuerpo_html: str, meta: dict) -> str:
    """Envuelve el cuerpo en la plantilla institucional completa."""
    veredicto = (meta.get("veredicto") or "REQUIERE_REVISION").upper()
    color, fondo, borde = VEREDICTO_ESTILO.get(veredicto, ("#555", "#f0f0f0", "#999"))

    sujeto  = meta.get("sujeto") or "Documento sin sujeto identificado"
    cedula  = meta.get("identificacion") or ""
    tipo    = meta.get("tipo") or ""
    subtipo = meta.get("subtipo") or ""
    asunto  = meta.get("asunto") or ""
    riesgo  = (meta.get("riesgo") or "").upper()
    fecha   = meta.get("fecha") or datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")

    chips = []
    if cedula:
        chips.append(f'<span class="chip"><b>C.C.</b> {html.escape(str(cedula))}</span>')
    if tipo:
        chips.append(f'<span class="chip"><b>Módulo</b> {html.escape(tipo)}</span>')
    if subtipo:
        chips.append(f'<span class="chip"><b>Subtipo</b> {html.escape(str(subtipo))}</span>')
    if riesgo:
        chips.append(f'<span class="chip"><b>Riesgo</b> {html.escape(riesgo)}</span>')
    chips.append(f'<span class="chip"><b>Fecha</b> {html.escape(fecha)}</span>')
    chips_html = "".join(chips)

    asunto_html = (
        f'<div class="asunto">{html.escape(asunto)}</div>' if asunto else ""
    )

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
    color:#fff; padding:26px 40px 22px;
    border-bottom:4px solid var(--azul-claro);
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
  .veredicto {{
    display:inline-block; margin-top:16px; padding:9px 26px;
    border-radius:5px; font-size:15px; font-weight:700; letter-spacing:1.1px;
    background:{fondo}; color:{color}; border:2px solid {borde};
  }}
  main {{
    max-width:940px; margin:26px auto 0; padding:34px 40px;
    background:#fff; border-radius:8px;
    box-shadow:0 1px 3px rgba(16,36,60,.09);
  }}
  h2.seccion {{
    font-size:14.5px; font-weight:700; color:var(--azul);
    text-transform:uppercase; letter-spacing:.6px;
    border-left:4px solid var(--azul-claro);
    background:#eef4fa; padding:9px 14px;
    margin:30px 0 12px; border-radius:0 5px 5px 0;
    display:flex; align-items:center; gap:10px;
  }}
  h2.seccion:first-child {{ margin-top:0; }}
  .num {{
    background:var(--azul-claro); color:#fff; font-size:11px;
    width:21px; height:21px; border-radius:50%;
    display:inline-flex; align-items:center; justify-content:center;
    flex-shrink:0;
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
  li {{
    position:relative; padding-left:18px; margin-bottom:6px;
  }}
  li::before {{
    content:""; position:absolute; left:2px; top:.62em;
    width:6px; height:6px; border-radius:50%; background:var(--azul-claro);
  }}
  li.li-bad::before {{ background:#b3261e; }}
  li.li-warn::before {{ background:#c98a00; }}
  li.li-ok::before  {{ background:#0f7b3d; }}
  .tabla-wrap {{
    overflow-x:auto; margin:12px 0 20px;
    border:1px solid var(--linea); border-radius:7px;
  }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  th {{
    background:var(--azul); color:#fff; text-align:left;
    padding:10px 14px; font-weight:600; font-size:12.5px;
    text-transform:uppercase; letter-spacing:.4px; white-space:nowrap;
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
  code {{
    background:#eef1f5; padding:1px 6px; border-radius:4px;
    font-family:Consolas,Monaco,monospace; font-size:12.5px;
  }}
  hr {{ border:0; border-top:1px solid var(--linea); margin:22px 0; }}
  footer {{
    max-width:940px; margin:18px auto 0; padding:0 40px;
    font-size:11px; color:#9aa5b1; text-align:center;
  }}
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
    <div class="veredicto">VEREDICTO: {html.escape(veredicto)}</div>
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
    """Punto de entrada: devuelve HTML o el texto crudo según FORMATO_SALIDA."""
    if FORMATO_SALIDA != "html":
        return analisis_texto
    try:
        return envolver_html(analisis_a_html_cuerpo(analisis_texto), meta)
    except Exception as e:
        print(f"[WARN] Falló el render HTML, se devuelve texto plano: {e}")
        return analisis_texto


# ══════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES
# ══════════════════════════════════════════════════════════════

def cargar_prompt(nombre: str) -> str:
    ruta = os.path.join(PROMPTS_DIR, f"{nombre}.txt")
    if not os.path.exists(ruta):
        ruta = os.path.join(PROMPTS_DIR, "general.txt")
    with open(ruta, "r", encoding="utf-8") as f:
        return f.read()


def subir_pdf(pdf_bytes: bytes, nombre: str) -> str:
    response = client.files.create(
        file=(nombre, pdf_bytes, "application/pdf"),
        purpose="user_data"
    )
    return response.id


def esperar_procesamiento(file_id: str, intentos: int = 15) -> bool:
    for _ in range(intentos):
        info = client.files.retrieve(file_id)
        if info.status == "processed":
            return True
        time.sleep(2)
    return False


def limpiar_archivos(file_ids: list):
    for fid in file_ids:
        try:
            client.files.delete(fid)
        except Exception:
            pass


def construir_content(file_ids: list, texto_prompt: str) -> list:
    content = []
    for fid in file_ids:
        content.append({"type": "file", "file": {"file_id": fid}})
    content.append({"type": "text", "text": texto_prompt})
    return content


def llamada_clasificador(file_ids: list) -> dict:
    """Clasifica el correo y detecta cuántos casos hay. Devuelve estructura multi-caso."""
    prompt = cargar_prompt("clasificador")
    content = construir_content(file_ids, prompt)

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": content}],
    )

    texto = response.choices[0].message.content.strip()

    if "```" in texto:
        partes = texto.split("```")
        for p in partes:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            try:
                return json.loads(p)
            except Exception:
                continue

    try:
        return json.loads(texto)
    except Exception:
        print(f"[WARN] No se pudo parsear clasificación: {texto[:500]}")
        return {
            "tipo": "OTRO",
            "dependencia": "DESCONOCIDO",
            "cantidad_casos": 1,
            "casos": [{
                "sujeto": None,
                "identificacion": None,
                "asunto": "No identificado",
                "radicado": None,
                "vencimiento": None,
                "riesgo": "MEDIO",
                "urgente": False,
                "subtipo": None,
                "indices_documentos": list(range(len(file_ids))),
                "documentos": []
            }],
            "documentos_huerfanos": []
        }


def llamada_analizador(file_ids_caso: list, tipo: str, caso: dict, tipo_general: str, dependencia: str) -> str:
    """Analiza UN caso específico con sus PDFs. file_ids_caso es solo los PDFs de ese caso."""
    nombre_prompt = MAPA_PROMPTS.get(tipo, "general")
    prompt = cargar_prompt(nombre_prompt)

    docs = caso.get('documentos', [])
    docs_texto = "\n".join(
        f"  - {d.get('nombre','?')} -> {d.get('rol','desconocido')}"
        for d in docs
    ) if docs else "  No se identificaron documentos individuales"

    subtipo = caso.get("subtipo")
    subtipo_linea = f"Subtipo (detectado por el clasificador): {subtipo}\n" if subtipo else ""

    contexto = (
        f"[CONTEXTO PREVIO DE CLASIFICACION]\n"
        f"Tipo: {tipo_general}\n"
        f"Dependencia: {dependencia}\n"
        f"{subtipo_linea}"
        f"Asunto: {caso.get('asunto', 'N/A')}\n"
        f"Sujeto: {caso.get('sujeto', 'N/A')}\n"
        f"Identificación: {caso.get('identificacion', 'N/A')}\n"
        f"Radicado: {caso.get('radicado', 'No identificado')}\n"
        f"Vencimiento: {caso.get('vencimiento', 'No identificado')}\n"
        f"Riesgo: {caso.get('riesgo', 'MEDIO')}\n"
        f"Urgente: {caso.get('urgente', False)}\n"
        f"Documentos de este caso:\n{docs_texto}\n\n"
        f"IMPORTANTE: Analiza SOLO el caso de {caso.get('sujeto', 'este docente/ciudadano')}. "
        f"Los PDFs que recibes son los que pertenecen exclusivamente a este caso. "
        f"El subtipo indicado arriba (si aplica) es una detección preliminar del clasificador: "
        f"verifícalo tú mismo contra la parte resolutiva del acto antes de darlo por definitivo.\n\n"
    )

    prompt_final = contexto + prompt
    content = construir_content(file_ids_caso, prompt_final)

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": content}],
    )

    return response.choices[0].message.content


def extraer_veredicto(texto: str) -> str:
    APROBADOS    = {"VEREDICTO: APROBADO"}
    DESAPROBADOS = {"VEREDICTO: DESAPROBADO", "VEREDICTO: REQUIERE_REVISION"}

    for linea in texto.strip().split("\n"):
        linea_norm = linea.strip().upper()
        if linea_norm in APROBADOS:
            return "APROBADO"
        if linea_norm in DESAPROBADOS:
            return "DESAPROBADO"

    texto_upper = texto.upper()
    if "VEREDICTO: APROBADO" in texto_upper:
        return "APROBADO"
    if "VEREDICTO: DESAPROBADO" in texto_upper or "VEREDICTO: REQUIERE_REVISION" in texto_upper:
        return "DESAPROBADO"

    print(f"[WARN] No se encontró veredicto explícito.")
    return "DESAPROBADO"


def limpiar_texto(texto: str) -> str:
    """Quita tildes y caracteres especiales para nombres de archivo."""
    if not texto:
        return ""
    texto = unicodedata.normalize('NFD', texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != 'Mn')
    texto = re.sub(r'[<>:"/\\|?*]', '', texto)
    texto = re.sub(r'\s+', ' ', texto)
    return texto.strip()


def construir_nombre_archivo(caso: dict, tipo: str, message_id: str) -> str:
    """Formato: SUJETO - IDENTIFICACION - TIPO - YYYY-MM-DD"""
    fecha = datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")
    sujeto = limpiar_texto(caso.get("sujeto") or "")
    identificacion = limpiar_texto(caso.get("identificacion") or "")

    if sujeto and identificacion:
        nombre = f"{sujeto} - {identificacion} - {tipo} - {fecha}"
    elif sujeto:
        nombre = f"{sujeto} - {tipo} - {fecha}"
    else:
        asunto = limpiar_texto(caso.get("asunto") or "Sin asunto")[:60]
        sufijo = message_id[-8:] if message_id else "sinid"
        nombre = f"{asunto} - {tipo} - {fecha} - {sufijo}"

    if len(nombre) > 180:
        nombre = nombre[:180]
    return nombre


def construir_advertencia_huerfanos(huerfanos: list, message_id: str) -> dict:
    """Genera un archivo de advertencia con los PDFs no emparejados."""
    fecha = datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")

    contenido = "DOCUMENTOS NO EMPAREJADOS\n\n"
    contenido += f"Correo de origen: {message_id}\n"
    contenido += f"Fecha de procesamiento: {fecha}\n\n"
    contenido += f"Se detectaron {len(huerfanos)} documento(s) que no pudieron asociarse a ningún caso.\n\n"

    contenido += "DETALLE\n\n"
    for h in huerfanos:
        contenido += f"- {h.get('nombre', 'Documento sin nombre')} — Razón: {h.get('razon', 'No especificada')}\n"

    contenido += "\nRECOMENDACION\n\n"
    contenido += "- Revisar el correo original y verificar que los soportes correspondan a un caso identificable.\n"
    contenido += "- Reenviar el expediente completo si faltan documentos principales.\n"

    meta = {
        "sujeto":        "Advertencia del sistema",
        "asunto":        "Documentos que no pudieron asociarse a ningún caso",
        "tipo":          "ADVERTENCIA",
        "veredicto":     "ADVERTENCIA",
        "fecha":         fecha,
    }

    return {
        "tipo":            "ADVERTENCIA",
        "carpeta":         "ADVERTENCIA",
        "nombre_archivo":  f"ADVERTENCIA - {message_id[-8:]} - {fecha}",
        "sujeto":          None,
        "identificacion":  None,
        "veredicto":       "ADVERTENCIA",
        "analisis":        renderizar_analisis(contenido, meta),
        "analisis_texto":  contenido,
        "message_id":      message_id,
        "cantidad_huerfanos": len(huerfanos)
    }


def limpiar_pendientes_vencidos():
    ahora = time.time()
    with lock_pendientes:
        vencidos = [
            mid for mid, datos in pendientes.items()
            if ahora - datos["timestamp"] > TTL_SEGUNDOS
        ]
        for mid in vencidos:
            print(f"[WARN] Descartando correo vencido: {mid}")
            del pendientes[mid]


def procesar_correo(message_id: str, archivos_datos: list) -> dict:
    """
    Procesa un correo completo con posiblemente varios casos.
    Devuelve un dict con 'resultados' que es lista de todos los análisis + advertencia si aplica.
    """
    file_ids = []
    try:
        # Subir todos los PDFs
        for archivo in archivos_datos:
            print(f"Subiendo {archivo['nombre']}...")
            fid = subir_pdf(archivo["bytes"], archivo["nombre"])
            file_ids.append(fid)
            print(f"  → {fid}")

        # Esperar procesamiento
        print("Esperando procesamiento de archivos...")
        for fid in file_ids:
            if not esperar_procesamiento(fid):
                raise Exception(f"Timeout esperando procesamiento de {fid}")

        # LLAMADA 1: Clasificar y detectar casos
        print("Clasificando documentos...")
        clasificacion = llamada_clasificador(file_ids)
        tipo_general  = clasificacion.get("tipo", "OTRO").strip().upper()
        dependencia   = (clasificacion.get("dependencia") or "DESCONOCIDO").strip().upper()
        casos         = clasificacion.get("casos", [])
        huerfanos     = clasificacion.get("documentos_huerfanos", [])

        # ── VALIDACIÓN DEFENSIVA ─────────────────────────────
        # 1. Filtrar índices fuera de rango en cada caso
        total_pdfs = len(file_ids)
        for caso in casos:
            indices_originales = caso.get("indices_documentos", [])
            indices_validos    = [i for i in indices_originales if isinstance(i, int) and 0 <= i < total_pdfs]
            if len(indices_validos) != len(indices_originales):
                print(f"  [WARN] Corrigiendo índices fuera de rango en caso '{caso.get('sujeto')}': "
                      f"{indices_originales} → {indices_validos}")
            caso["indices_documentos"] = indices_validos

        # 2. Deduplicar casos con mismo sujeto+identificación
        casos_unicos = {}
        for caso in casos:
            clave = (caso.get("sujeto"), caso.get("identificacion"))
            if clave in casos_unicos:
                # Fusionar índices sin duplicar
                indices_existentes = set(casos_unicos[clave]["indices_documentos"])
                indices_nuevos     = set(caso.get("indices_documentos", []))
                casos_unicos[clave]["indices_documentos"] = sorted(indices_existentes | indices_nuevos)
                print(f"  [WARN] Fusionando caso duplicado de '{caso.get('sujeto')}'")
            else:
                casos_unicos[clave] = caso
        casos = list(casos_unicos.values())

        # 3. Filtrar casos sin documentos válidos
        casos = [c for c in casos if c.get("indices_documentos")]

        print(f"Tipo general: {tipo_general} | Casos detectados: {len(casos)} | Huérfanos: {len(huerfanos)}")

        resultados = []
        fecha_hoy = datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d")

        # LLAMADA 2..N: Analizar cada caso por separado
        for i, caso in enumerate(casos, start=1):
            sujeto = caso.get('sujeto', 'sin_nombre')
            print(f"[{i}/{len(casos)}] Analizando caso de: {sujeto}")

            # Extraer solo los file_ids de este caso
            indices = caso.get("indices_documentos", [])
            print(f"  Indices del clasificador: {indices} (total PDFs disponibles: {len(file_ids)})")
            file_ids_caso = [file_ids[idx] for idx in indices if 0 <= idx < len(file_ids)]
            print(f"  PDFs asignados a este caso: {len(file_ids_caso)}")

            if not file_ids_caso:
                print(f"  [WARN] Caso sin documentos válidos, saltando: {sujeto}")
                continue

            # Ejecutar análisis
            analisis  = llamada_analizador(file_ids_caso, tipo_general, caso, tipo_general, dependencia)
            veredicto = extraer_veredicto(analisis)
            carpeta   = MAPA_CARPETAS.get((tipo_general, veredicto), "OTRO")
            nombre    = construir_nombre_archivo(caso, tipo_general, message_id)

            print(f"  Veredicto: {veredicto} | Carpeta: {carpeta}")

            # ── Metadatos para el encabezado del HTML ──
            meta_html = {
                "sujeto":         caso.get("sujeto"),
                "identificacion": caso.get("identificacion"),
                "tipo":           tipo_general,
                "subtipo":        caso.get("subtipo"),
                "asunto":         (caso.get("asunto") or "").strip(),
                "riesgo":         (caso.get("riesgo") or "MEDIO").strip().upper(),
                "veredicto":      veredicto,
                "fecha":          fecha_hoy,
            }

            resultados.append({
                "tipo":            tipo_general,
                "dependencia":     dependencia,
                "subtipo":         caso.get("subtipo"),
                "asunto":          (caso.get("asunto") or "").strip(),
                "sujeto":          caso.get("sujeto"),
                "identificacion":  caso.get("identificacion"),
                "radicado":        caso.get("radicado"),
                "vencimiento":     caso.get("vencimiento"),
                "riesgo":          (caso.get("riesgo") or "MEDIO").strip().upper(),
                "urgente":         caso.get("urgente", False),
                "veredicto":       veredicto,
                "carpeta":         carpeta,
                "nombre_archivo":  nombre,
                "analisis":        renderizar_analisis(analisis, meta_html),
                "analisis_texto":  analisis,
                "message_id":      message_id
            })

        # Agregar advertencia si hay huérfanos
        if huerfanos:
            print(f"[!] Generando advertencia con {len(huerfanos)} documentos huérfanos")
            resultados.append(construir_advertencia_huerfanos(huerfanos, message_id))

        return {
            "message_id":       message_id,
            "tipo_general":     tipo_general,
            "cantidad_casos":   len(casos),
            "cantidad_huerfanos": len(huerfanos),
            "archivos_procesados": len(file_ids),
            "formato_salida":   FORMATO_SALIDA,
            "resultados":       resultados
        }

    finally:
        limpiar_archivos(file_ids)


# ── Endpoints de diagnóstico ───────────────────────────────────

@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "version":         BUILD_VERSION,
        "build_date":      BUILD_DATE,
        "fix":             BUILD_FIX,
        "model":           MODEL,
        "formato_salida":  FORMATO_SALIDA,
        "status":          "ok"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": BUILD_VERSION})


@app.route("/preview", methods=["GET", "POST"])
def preview():
    """
    Vista previa del diseño HTML sin gastar llamadas a OpenAI.
    GET  → renderiza un análisis de ejemplo.
    POST → recibe {"analisis": "...", "meta": {...}} y devuelve el HTML.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        texto = data.get("analisis", "")
        meta  = data.get("meta", {})
        return renderizar_analisis(texto, meta), 200, {"Content-Type": "text/html; charset=utf-8"}

    # AVISO: datos ficticios, usados únicamente para previsualizar el diseño.
    ejemplo = """1. RESUMEN DEL CASO

Docente: **NOMBRE APELLIDO DE PRUEBA**
Cédula: **00.000.000**
Subtipo detectado: **cesantia_parcial_remodelacion_vivienda**
Inmueble: Calle 00 No. 00-00, Barrio de Prueba, Ciudad
Valor reconocido en el acto: **$00.000.000**

2. SUBTIPO IDENTIFICADO

Se clasifica como remodelación porque los soportes incluyen contrato civil de obra,
certificado de tradición y documentos del contratista. No hay soportes educativos.

3. MATRIZ DE IDENTIDAD

| Campo | En el acto | En la cédula del docente | Resultado |
|---|---|---|---|
| Nombre | NOMBRE APELLIDO DE PRUEBA | NOMBRE APELLIDO DE PRUEBA | coincide |
| Cédula | 00.000.000 | 00.000.000 | coincide |

4. MATRIZ DE VALORES

| Concepto | Valor soportado | Valor reconocido | Resultado |
|---|---|---|---|
| Valor del contrato de obra | $00.000.000 | $00.000.000 | coincide |
| Saldo a pagar | $00.000.000 | $00.000.000 | coincide |

5. MATRIZ DE CUENTA BANCARIA

| Titular | Banco | Tipo de cuenta | Número | Coincide con beneficiario | Resultado |
|---|---|---|---|---|---|
| NOMBRE APELLIDO DE PRUEBA | Banco de prueba | Cuenta de ahorro | 000-000000-00 | Sí | coincide |

6. MATRIZ INMUEBLE

| Matrícula | Dirección | Titular | Docente es propietario | Resultado |
|---|---|---|---|---|
| 000-000000 | Calle 00 No. 00-00 | Antecedentes de dominio de terceros | Sí | coincide_con_validacion_manual |

7. MATRIZ OBRA

| Contratante | Contratista | Objeto | Valor del contrato | Resultado |
|---|---|---|---|---|
| NOMBRE APELLIDO DE PRUEBA | CONTRATISTA DE PRUEBA | Remodelación de vivienda | $00.000.000 | coincide |

8. DOCUMENTOS FALTANTES

| Documento | Carácter | Estado |
|---|---|---|
| Acto administrativo | Obligatorio | aportado |
| Cédula del docente | Obligatorio | aportado |
| Tarjeta profesional del contratista | Complementario | no_aplica |
| Soporte de parentesco | No aplica al subtipo | no_aplica |

9. RIESGOS DETECTADOS

- ALTO: no se detectaron riesgos de nivel alto.
- MEDIO: el certificado de tradición registra antecedentes de terceros.
- BAJO: la cédula del contratista está correctamente asociada al contrato.

10. RECOMENDACION FINAL

VIABLE CON VALIDACIÓN MANUAL: el expediente está bien estructurado y solo requiere
confirmar la situación dominial actual del inmueble.

11. NOTA PARA EL ABOGADO REVISOR

Documento de demostración generado con datos ficticios. No corresponde a ningún
expediente real ni a ninguna persona identificable.

VEREDICTO: APROBADO"""

    meta = {
        "sujeto": "NOMBRE APELLIDO DE PRUEBA",
        "identificacion": "00000000",
        "tipo": "CESANTIAS",
        "subtipo": "cesantia_parcial_remodelacion_vivienda",
        "asunto": "Vista previa del formato — datos ficticios de demostración",
        "riesgo": "BAJO",
        "veredicto": "APROBADO",
        "fecha": datetime.now(TZ_COLOMBIA).strftime("%Y-%m-%d"),
    }
    return envolver_html(analisis_a_html_cuerpo(ejemplo), meta), 200, {
        "Content-Type": "text/html; charset=utf-8"
    }


# ── Endpoint principal ─────────────────────────────────────────

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
            pendientes[message_id]["archivos"].append({
                "bytes":  archivo.read(),
                "nombre": archivo.filename or "documento.pdf"
            })

        recibidos = len(pendientes[message_id]["archivos"])

    print(f"[{message_id}] Recibidos {recibidos}/{total_files} archivos")

    if recibidos < total_files:
        return jsonify({
            "status": "acumulando",
            "recibidos": recibidos,
            "esperados": total_files,
            "message_id": message_id
        }), 202

    with lock_pendientes:
        datos_correo = pendientes.pop(message_id)["archivos"]

    try:
        resultado = procesar_correo(message_id, datos_correo)
        return jsonify(resultado), 200
    except Exception as e:
        print(f"Error procesando {message_id}: {str(e)}")
        return jsonify({"error": str(e), "message_id": message_id}), 500


# ── Arranque local ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)