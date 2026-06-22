"""
MICROSERVICIO JURÍDICO v2.4
Arquitectura de doble llamada: Clasificador → Analizador especializado
Acumulación de PDFs por message_id para recibir múltiples archivos del mismo correo.
"""

import os
import re
import time
import threading
import json
import unicodedata
from datetime import datetime
from flask import Flask, request, jsonify
import openai
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Versión del build ──────────────────────────────────────────
BUILD_VERSION = "2.4"
BUILD_DATE    = "2026-05-26"
BUILD_FIX     = "Nombre de archivo descriptivo con sujeto e identificación"

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
        print(f"[WARN] No se pudo parsear clasificación: {texto}")
        return {
            "tipo": "OTRO",
            "dependencia": "DESCONOCIDO",
            "asunto": "No identificado",
            "sujeto": None,
            "identificacion": None,
            "radicado": None,
            "vencimiento": None,
            "riesgo": "MEDIO",
            "urgente": False,
            "cantidad_casos": 1,
            "documentos": []
        }


def llamada_analizador(file_ids: list, tipo: str, clasificacion: dict) -> str:
    nombre_prompt = MAPA_PROMPTS.get(tipo, "general")
    prompt = cargar_prompt(nombre_prompt)

    docs = clasificacion.get('documentos', [])
    docs_texto = "\n".join(
        f"  - {d.get('nombre','?')} -> {d.get('rol','desconocido')}"
        for d in docs
    ) if docs else "  No se identificaron documentos individuales"

    contexto = (
        f"[CONTEXTO PREVIO DE CLASIFICACION]\n"
        f"Tipo: {clasificacion.get('tipo', 'N/A')}\n"
        f"Dependencia: {clasificacion.get('dependencia', 'N/A')}\n"
        f"Asunto: {clasificacion.get('asunto', 'N/A')}\n"
        f"Sujeto: {clasificacion.get('sujeto', 'N/A')}\n"
        f"Identificación: {clasificacion.get('identificacion', 'N/A')}\n"
        f"Radicado: {clasificacion.get('radicado', 'No identificado')}\n"
        f"Vencimiento: {clasificacion.get('vencimiento', 'No identificado')}\n"
        f"Riesgo: {clasificacion.get('riesgo', 'MEDIO')}\n"
        f"Urgente: {clasificacion.get('urgente', False)}\n"
        f"Casos en este correo: {clasificacion.get('cantidad_casos', 1)}\n"
        f"Documentos identificados:\n{docs_texto}\n\n"
        f"IMPORTANTE: Usa el rol de cada documento para orientar tu analisis.\n\n"
    )

    prompt_final = contexto + prompt
    content = construir_content(file_ids, prompt_final)

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

    print(f"[WARN] No se encontró veredicto explícito. Últimas 3 líneas: "
          f"{texto.strip().split(chr(10))[-3:]}")
    return "DESAPROBADO"


def limpiar_texto(texto: str) -> str:
    """Quita tildes y caracteres especiales para nombres de archivo."""
    if not texto:
        return ""
    # Normaliza tildes (NFD descompone, luego filtra los acentos)
    texto = unicodedata.normalize('NFD', texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != 'Mn')
    # Elimina caracteres no compatibles con nombres de archivo
    texto = re.sub(r'[<>:"/\\|?*]', '', texto)
    # Colapsa múltiples espacios
    texto = re.sub(r'\s+', ' ', texto)
    return texto.strip()


def construir_nombre_archivo(clasificacion: dict, tipo: str, veredicto: str, message_id: str) -> str:
    """
    Construye nombre descriptivo del archivo Dropbox.
    Formato: SUJETO - IDENTIFICACION - TIPO - YYYY-MM-DD.txt
    Si falta sujeto o identificación, usa fallback con asunto y message_id.
    """
    fecha = datetime.now().strftime("%Y-%m-%d")
    sujeto = limpiar_texto(clasificacion.get("sujeto") or "")
    identificacion = limpiar_texto(clasificacion.get("identificacion") or "")

    if sujeto and identificacion:
        nombre = f"{sujeto} - {identificacion} - {tipo} - {fecha}"
    elif sujeto:
        nombre = f"{sujeto} - {tipo} - {fecha}"
    else:
        # Fallback: usar asunto + parte del message_id
        asunto = limpiar_texto(clasificacion.get("asunto") or "Sin asunto")[:60]
        sufijo = message_id[-8:] if message_id else "sinid"
        nombre = f"{asunto} - {tipo} - {fecha} - {sufijo}"

    # Limitar longitud total (Dropbox máx 255, pero usamos 180 por seguridad)
    if len(nombre) > 180:
        nombre = nombre[:180]

    return nombre


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
    file_ids = []
    try:
        for archivo in archivos_datos:
            print(f"Subiendo {archivo['nombre']}...")
            fid = subir_pdf(archivo["bytes"], archivo["nombre"])
            file_ids.append(fid)
            print(f"  → {fid}")

        print("Esperando procesamiento de archivos...")
        for fid in file_ids:
            if not esperar_procesamiento(fid):
                raise Exception(f"Timeout esperando procesamiento de {fid}")

        print("Clasificando documentos...")
        clasificacion = llamada_clasificador(file_ids)
        tipo = clasificacion.get("tipo", "OTRO").strip().upper()
        print(f"Tipo: {tipo} | Sujeto: {clasificacion.get('sujeto')} | ID: {clasificacion.get('identificacion')}")

        print(f"Analizando con prompt: {MAPA_PROMPTS.get(tipo, 'general')}...")
        analisis = llamada_analizador(file_ids, tipo, clasificacion)

        veredicto = extraer_veredicto(analisis)
        carpeta   = MAPA_CARPETAS.get((tipo, veredicto), "OTRO")
        nombre_archivo = construir_nombre_archivo(clasificacion, tipo, veredicto, message_id)
        print(f"Veredicto: {veredicto} | Carpeta: {carpeta} | Archivo: {nombre_archivo}")

        return {
            "tipo":                tipo,
            "dependencia":         (clasificacion.get("dependencia") or "DESCONOCIDO").strip().upper(),
            "asunto":              (clasificacion.get("asunto") or "").strip(),
            "sujeto":              clasificacion.get("sujeto"),
            "identificacion":      clasificacion.get("identificacion"),
            "radicado":            clasificacion.get("radicado"),
            "vencimiento":         clasificacion.get("vencimiento"),
            "riesgo":              (clasificacion.get("riesgo") or "MEDIO").strip().upper(),
            "urgente":             clasificacion.get("urgente", False),
            "veredicto":           veredicto,
            "carpeta":             carpeta,
            "nombre_archivo":      nombre_archivo,
            "analisis":            analisis,
            "message_id":          message_id,
            "archivos_procesados": len(file_ids)
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