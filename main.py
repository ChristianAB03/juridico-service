"""
MICROSERVICIO JURÍDICO v3.0
Arquitectura multi-caso: un correo puede contener varios casos del mismo tipo.
Flujo: Clasificador identifica N casos → Analizador se ejecuta N veces → Devuelve resultados[].
"""

import os
import re
import time
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
BUILD_VERSION = "3.1"
BUILD_DATE    = "2026-05-26"
BUILD_FIX     = "Zona horaria Colombia + multi-caso"

# ── Configuración ──────────────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
API_SECRET     = os.environ.get("API_SECRET", "clave_secreta_make")
MODEL          = "gpt-5.4-mini-2026-03-17"

client = openai.OpenAI(api_key=OPENAI_API_KEY)

# ── Acumulador de PDFs por correo ──────────────────────────────
pendientes = {}
lock_pendientes = threading.Lock()
TTL_SEGUNDOS = 300

# ── Mapa de tipos a archivos de prompt ────────────────────────
MAPA_PROMPTS = {
    "RESOLUCION":     "resolucion",
    "RETIRO_FORZOSO": "retiro_forzoso",
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
    ("TUTELA",         "APROBADO"):    "TUTELA_APROBADO",
    ("TUTELA",         "DESAPROBADO"): "TUTELA_DESAPROBADO",
    ("PETICION",       "APROBADO"):    "PETICION_APROBADO",
    ("PETICION",       "DESAPROBADO"): "PETICION_DESAPROBADO",
    ("REQUERIMIENTO",  "APROBADO"):    "REQUERIMIENTO_APROBADO",
    ("REQUERIMIENTO",  "DESAPROBADO"): "REQUERIMIENTO_DESAPROBADO",
}

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


# ── Funciones auxiliares ───────────────────────────────────────

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

    contexto = (
        f"[CONTEXTO PREVIO DE CLASIFICACION]\n"
        f"Tipo: {tipo_general}\n"
        f"Dependencia: {dependencia}\n"
        f"Asunto: {caso.get('asunto', 'N/A')}\n"
        f"Sujeto: {caso.get('sujeto', 'N/A')}\n"
        f"Identificación: {caso.get('identificacion', 'N/A')}\n"
        f"Radicado: {caso.get('radicado', 'No identificado')}\n"
        f"Vencimiento: {caso.get('vencimiento', 'No identificado')}\n"
        f"Riesgo: {caso.get('riesgo', 'MEDIO')}\n"
        f"Urgente: {caso.get('urgente', False)}\n"
        f"Documentos de este caso:\n{docs_texto}\n\n"
        f"IMPORTANTE: Analiza SOLO el caso de {caso.get('sujeto', 'este docente/ciudadano')}. "
        f"Los PDFs que recibes son los que pertenecen exclusivamente a este caso.\n\n"
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

    contenido = f"ADVERTENCIA - DOCUMENTOS NO EMPAREJADOS\n"
    contenido += f"Correo: {message_id}\n"
    contenido += f"Fecha: {fecha}\n\n"
    contenido += f"Se detectaron {len(huerfanos)} documento(s) que no pudieron asociarse a ningún caso:\n\n"

    for h in huerfanos:
        contenido += f"- {h.get('nombre', 'Documento sin nombre')}\n"
        contenido += f"  Razón: {h.get('razon', 'No especificada')}\n\n"

    contenido += "\nSe recomienda revisar el correo original y enviar los documentos completos si es necesario.\n"

    return {
        "tipo":            "ADVERTENCIA",
        "carpeta":         "ADVERTENCIA",
        "nombre_archivo":  f"ADVERTENCIA - {message_id[-8:]} - {fecha}",
        "sujeto":          None,
        "identificacion":  None,
        "veredicto":       "ADVERTENCIA",
        "analisis":        contenido,
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

        print(f"Tipo general: {tipo_general} | Casos detectados: {len(casos)} | Huérfanos: {len(huerfanos)}")

        resultados = []

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

            resultados.append({
                "tipo":            tipo_general,
                "dependencia":     dependencia,
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
                "analisis":        analisis,
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
            "resultados":       resultados
        }

    finally:
        limpiar_archivos(file_ids)


# ── Endpoints de diagnóstico ───────────────────────────────────

@app.route("/version", methods=["GET"])
def version():
    return jsonify({
        "version":    BUILD_VERSION,
        "build_date": BUILD_DATE,
        "fix":        BUILD_FIX,
        "model":      MODEL,
        "status":     "ok"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": BUILD_VERSION})


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