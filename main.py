"""
MICROSERVICIO JURÍDICO
Recibe PDFs desde Make, los sube a OpenAI, y devuelve análisis jurídico.
"""

from flask import Flask, request, jsonify
import openai
import os
import base64
import time
import re

app = Flask(__name__)

def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


load_env()
# ── Configuración ──────────────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
API_SECRET     = os.environ.get("API_SECRET", "clave_secreta_make")  # para proteger el endpoint
MODEL          = "gpt-5.4-mini-2026-03-17"

client = openai.OpenAI(api_key=OPENAI_API_KEY)

# ── Prompt jurídico completo ───────────────────────────────────
PROMPT_JURIDICO = """Actúa como abogado experto en derecho administrativo educativo, carrera docente, planta de personal docente y control de legalidad de actos administrativos expedidos por una Secretaría de Educación certificada en Colombia.

Voy a adjuntarte un proyecto de acto administrativo mediante el cual se pretende realizar una reubicación de un docente por necesidad del servicio. El acto puede referirse a:
Reubicación de cargo docente, por ejemplo, de docente de aula a docente orientador, o viceversa.
Reubicación por perfil, área, asignación académica o necesidad del servicio, por ejemplo, de docente de aula primaria a docente de aula matemáticas, ciencias naturales, inglés u otra área.
También puedo adjuntar soportes como formato único de novedades, solicitud del docente, hoja de vida, títulos académicos, certificado de planta, necesidad del servicio, concepto del rector o de Gestión Administrativa Docente.
Tu tarea es revisar si el acto administrativo está bien proyectado, si la figura jurídica utilizada corresponde al caso concreto y si la reubicación se encuentra debidamente soportada.

1. Identificación inicial del acto
Primero identifica:
Tipo de acto administrativo.
Nombre del docente.
Cargo actual.
Perfil, área o cargo actual.
Institución educativa actual.
Cargo, perfil o área al que se pretende reubicar.
Institución educativa de destino, si cambia.
Si el acto corresponde realmente a una reubicación de cargo o a una reubicación/cambio de perfil dentro del mismo cargo docente.
Si el acto se fundamenta en solicitud del docente, necesidad del servicio, decisión administrativa o una combinación de estas.

2. Revisión de competencia
Verifica si la autoridad que firma el acto tiene competencia para expedirlo.
Analiza especialmente:
Ley 115 de 1994, artículo 153.
Ley 715 de 2001, artículo 7, especialmente numeral 7.3.
Decreto 1075 de 2015, normas sobre planta, cargos docentes y manual de funciones.
Decreto Distrital 0208 de 2016, si aplica.
Decreto Distrital 0090 de 2026, si aplica.
Cualquier acto de delegación, reasunción o distribución de funciones entre Alcalde y Secretaría de Educación.
Determina si el acto puede ser firmado por la Secretaría de Educación o si, por su naturaleza, podría requerir firma del Alcalde como autoridad nominadora.
Distingue si se trata de una decisión propia de administración del servicio educativo, una modificación de asignación o perfil, o una actuación que compromete directamente la función nominadora.

3. Revisión de la figura jurídica usada
Analiza si la figura utilizada en el título y en la parte motiva corresponde al caso concreto.
Diferencia claramente:
Reubicación de cargo docente:
Cuando el docente pasa de un cargo a otro dentro del sistema especial docente, por ejemplo, de docente de aula a docente orientador, o de docente orientador a docente de aula. En este caso debe revisarse el artículo 2.4.6.3.4 del Decreto 1075 de 2015, modificado por el Decreto 2105 de 2017.
Reubicación por perfil, área o asignación:
Cuando el docente sigue siendo docente de aula, pero cambia el área, nivel, perfil o asignación académica, por ejemplo, de primaria a matemáticas. En este caso debe verificarse si el acto está usando correctamente la norma de reubicación de cargo o si requiere una motivación distinta basada en la administración de la planta, necesidad del servicio, manual de funciones, perfil profesional y organización del servicio educativo.
Indica si existe riesgo jurídico por llamar reubicación de cargo a lo que realmente parece ser una modificación de perfil o área de desempeño.

4. Revisión normativa
Extrae todas las normas citadas en el acto administrativo y clasifícalas así:
Normas de competencia.
Normas sobre administración del servicio educativo.
Normas sobre planta docente.
Normas sobre reubicación de cargo.
Normas sobre manual de funciones, requisitos y perfiles.
Normas sobre notificación, comunicación y recursos.
Luego revisa:
Si las normas están vigentes.
Si son pertinentes para el caso.
Si falta alguna norma relevante.
Si alguna norma está mal citada, incompleta o usada fuera de contexto.
Si el Decreto 1075 de 2015 se cita con el artículo correcto.
Si la Resolución MEN 03842 de 2022 corresponde al cargo o perfil específico que se pretende asignar.

5. Revisión de soportes
Verifica si el expediente contiene, como mínimo, los siguientes soportes:
Solicitud escrita del docente, cuando la reubicación se presenta a petición de parte.
Formato único de novedades debidamente diligenciado.
Documento de identidad o identificación plena del docente.
Acto de nombramiento o información que demuestre el cargo actual.
Constancia de que el docente tiene derechos de carrera, si se invoca la reubicación del artículo 2.4.6.3.4 del Decreto 1075 de 2015.
Certificación o verificación de títulos académicos.
Verificación del cumplimiento de requisitos mínimos del cargo o perfil de destino.
Soporte de necesidad del servicio.
Certificación de existencia de vacante o necesidad dentro de la planta, si aplica.
Concepto o aval de la dependencia competente.
Verificación de que la reubicación no afecta derechos de carrera, escalafón, remuneración ni estabilidad del docente.
Evidencia de que el cambio es funcional, necesario y razonable.
Indica si los soportes son suficientes o si debe requerirse información adicional antes de firmar el acto.

6. Revisión de motivación
Evalúa si la motivación del acto es suficiente.
Revisa si el acto explica:
Por qué existe necesidad del servicio.
Por qué el docente cumple el perfil o requisitos del nuevo cargo o área.
Por qué la reubicación resulta procedente.
Si el cambio beneficia la prestación del servicio educativo.
Si existe coherencia entre la solicitud, los soportes y la decisión.
Si la motivación es concreta o si se limita a fórmulas generales.
Si se diferencia adecuadamente entre cargo, perfil, área, asignación académica y establecimiento educativo.
Sugiere redacciones para fortalecer la motivación, evitando afirmaciones genéricas.

7. Revisión de la parte resolutiva
Analiza si los artículos del acto son claros y completos.
Verifica si la parte resolutiva identifica correctamente:
Nombre completo del docente.
Cédula.
Cargo actual.
Institución educativa actual.
Cargo, perfil o área al que se reubica.
Institución educativa donde prestará el servicio.
Fecha a partir de la cual rige la decisión.
Dependencias a las que debe comunicarse.
Efectos administrativos, salariales y de carrera.
Si procede o no recurso.
Advierte si debe precisarse que la medida no implica pérdida de derechos de carrera, modificación del escalafón ni desmejora laboral.

8. Revisión de recursos y notificación
Verifica si el acto indica correctamente la forma de comunicación o notificación.
Analiza:
Si se debe comunicar o notificar personalmente.
Si se cita correctamente el artículo 67 y siguientes de la Ley 1437 de 2011.
Si la frase contra la presente resolución no proceden recursos es jurídicamente adecuada.
Si, por tratarse de acto particular que afecta o define una situación individual, debe concederse recurso de reposición o apelación, o si puede sustentarse que se trata de un acto de administración interna o de ejecución de una solicitud aceptada.
Si encuentras duda sobre recursos, advierte el riesgo y sugiere una fórmula más segura.

9. Riesgos jurídicos
Identifica riesgos como:
Falta de competencia del firmante.
Uso equivocado de la figura de reubicación de cargo para un simple cambio de perfil.
Falta de soporte de necesidad del servicio.
Falta de verificación del cumplimiento de requisitos del cargo o perfil.
Ausencia de constancia sobre derechos de carrera.
Posible afectación de derechos del docente.
Falta de motivación suficiente.
Confusión entre traslado, reubicación de cargo, reubicación por perfil y asignación académica.
Problemas con recursos o notificación.
Riesgo de que el acto sea demandado por falsa motivación, falta de competencia, desviación de poder o expedición irregular.

10. Resultado esperado
Entrega el análisis en el siguiente formato:
A. Diagnóstico general
Indica si el acto está jurídicamente viable, viable con ajustes o no recomendable para firma.
B. Tipo real de actuación
Precisa si se trata de reubicación de cargo, reubicación por perfil, cambio de área, asignación funcional, traslado o una figura mixta.
C. Normas citadas y evaluación
Haz una tabla con norma, finalidad dentro del acto y observación jurídica.
D. Soportes revisados
Indica cuáles soportes aparecen, cuáles faltan y cuáles deben verificarse.
E. Observaciones de fondo
Enumera los problemas jurídicos relevantes.
F. Observaciones de forma y redacción
Señala errores de redacción, coherencia, título, considerandos y parte resolutiva.
G. Ajustes recomendados
Propón cambios concretos al acto.
H. Redacción sugerida
Incluye textos sugeridos para mejorar los considerandos y la parte resolutiva.
I. Concepto final para el abogado revisor
Redacta una recomendación breve, técnica y prudente, como si fuera una nota interna para quien proyectó el acto.

No inventes información que no esté en el expediente. Si falta un soporte, indícalo expresamente. Si no puedes verificar un dato, usa fórmulas como debe verificarse, debe acreditarse en el expediente o no se observa soporte suficiente en los documentos revisados.

Responde en texto plano, sin asteriscos, sin símbolos markdown, sin ##, sin ---. Usa solo MAYÚSCULAS para títulos y guiones simples para listas.

Al final de tu respuesta, escribe obligatoriamente una de estas tres líneas exactas, sin variaciones:
VEREDICTO: APROBADO
VEREDICTO: DESAPROBADO
VEREDICTO: REQUIERE_REVISION"""


# ── Funciones auxiliares ───────────────────────────────────────

def subir_pdf_a_openai(pdf_bytes: bytes, nombre: str) -> str:
    """Sube un PDF a OpenAI Files API y devuelve el file_id."""
    response = client.files.create(
        file=(nombre, pdf_bytes, "application/pdf"),
        purpose="user_data"
    )
    return response.id


def esperar_procesamiento(file_id: str, intentos: int = 10) -> bool:
    """Espera hasta que OpenAI procese el archivo."""
    for _ in range(intentos):
        file_info = client.files.retrieve(file_id)
        if file_info.status == "processed":
            return True
        time.sleep(2)
    return False


def analizar_documentos(file_ids: list[str]) -> str:
    """Llama a OpenAI con todos los file_ids y devuelve el análisis."""
    content = []

    # Agrega cada archivo como objeto file
    for fid in file_ids:
        content.append({
            "type": "file",
            "file": {"file_id": fid}
        })

    # Agrega el prompt jurídico al final
    content.append({
        "type": "text",
        "text": PROMPT_JURIDICO
    })

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "user", "content": content}
        ]
    )

    return response.choices[0].message.content


def extraer_veredicto(texto: str) -> str:
    """Extrae el veredicto del texto de respuesta."""
    for linea in texto.strip().split("\n"):
        linea = linea.strip()
        if linea in ["VEREDICTO: APROBADO", "VEREDICTO: DESAPROBADO", "VEREDICTO: REQUIERE_REVISION"]:
            return linea.replace("VEREDICTO: ", "")
    # Si no encontró línea exacta, busca por contenido
    texto_upper = texto.upper()
    if "VEREDICTO: APROBADO" in texto_upper:
        return "APROBADO"
    elif "VEREDICTO: DESAPROBADO" in texto_upper:
        return "DESAPROBADO"
    else:
        return "REQUIERE_REVISION"


def limpiar_archivos(file_ids: list[str]):
    """Elimina los archivos de OpenAI después de usarlos."""
    for fid in file_ids:
        try:
            client.files.delete(fid)
        except Exception:
            pass  # Si falla la limpieza no es crítico


# ── Endpoint principal ─────────────────────────────────────────

@app.route("/analizar", methods=["POST"])
def analizar():
    """
    Recibe PDFs desde Make y devuelve el análisis jurídico.

    Make debe enviar:
    - Header: X-API-Secret: <API_SECRET>
    - Form-data con uno o más campos 'pdf' (archivos binarios)
    - Opcionalmente: campo 'message_id' como referencia
    """

    # Verificar autenticación
    secret = request.headers.get("X-API-Secret")
    if secret != API_SECRET:
        return jsonify({"error": "No autorizado"}), 401

    # Verificar que llegaron archivos
    archivos = request.files.getlist("pdf")
    if not archivos:
        return jsonify({"error": "No se recibieron archivos PDF"}), 400

    message_id = request.form.get("message_id", "sin_id")
    file_ids = []

    try:
        # 1. Subir cada PDF a OpenAI
        for archivo in archivos:
            pdf_bytes = archivo.read()
            nombre = archivo.filename or "documento.pdf"
            print(f"Subiendo {nombre}...")

            file_id = subir_pdf_a_openai(pdf_bytes, nombre)
            file_ids.append(file_id)
            print(f"  → {file_id}")

        # 2. Esperar que OpenAI procese todos los archivos
        print("Esperando procesamiento...")
        for fid in file_ids:
            procesado = esperar_procesamiento(fid)
            if not procesado:
                raise Exception(f"Timeout esperando procesamiento de {fid}")

        # 3. Analizar todos los documentos juntos
        print(f"Analizando {len(file_ids)} documentos...")
        analisis = analizar_documentos(file_ids)

        # 4. Extraer veredicto
        veredicto = extraer_veredicto(analisis)
        print(f"Veredicto: {veredicto}")

        # 5. Limpiar archivos de OpenAI
        limpiar_archivos(file_ids)

        return jsonify({
            "veredicto": veredicto,
            "analisis": analisis,
            "message_id": message_id,
            "archivos_procesados": len(file_ids)
        })

    except Exception as e:
        # Si algo falla, limpiar archivos y reportar error
        limpiar_archivos(file_ids)
        print(f"Error: {str(e)}")
        return jsonify({
            "error": str(e),
            "message_id": message_id
        }), 500


# ── Health check ───────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ── Arranque local ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
