# Microservicio Jurídico
Analiza actos administrativos educativos usando OpenAI.

## Estructura
```
juridico_service/
├── main.py           # Microservicio principal
├── requirements.txt  # Dependencias Python
├── Procfile          # Para Railway/Render
├── .env.example      # Variables de entorno de ejemplo
└── test_local.py     # Script de prueba local
```

## Instalación local

```bash
# 1. Crear entorno virtual
python3 -m venv venv
source venv/bin/activate  # Mac/Linux
venv\Scripts\activate     # Windows

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Crear archivo .env
cp .env.example .env
# Editar .env con tus valores reales

# 4. Arrancar el servidor
python main.py
```

## Prueba local

```bash
python test_local.py resolucion.pdf soporte.pdf
```

## Despliegue en Railway

1. Crear cuenta en https://railway.app
2. Nuevo proyecto → Deploy from GitHub
3. Subir estos archivos a un repositorio privado de GitHub
4. En Railway → Variables → agregar:
   - OPENAI_API_KEY = tu_clave_openai
   - API_SECRET = tu_clave_secreta
5. Railway detecta el Procfile y despliega automáticamente
6. Copiar la URL pública que Railway asigna

## Endpoint

POST /analizar
Headers:
  X-API-Secret: <API_SECRET>
Body (multipart/form-data):
  pdf: <archivo1.pdf>
  pdf: <archivo2.pdf>
  message_id: <id_referencia>

Respuesta exitosa:
{
  "veredicto": "APROBADO" | "DESAPROBADO" | "REQUIERE_REVISION",
  "analisis": "texto completo del análisis jurídico",
  "message_id": "id_referencia",
  "archivos_procesados": 2
}

## Configuración en Make

Ver sección FASE 3 de las instrucciones de implementación.
