"""
Script para probar el microservicio.
Uso: python test_local.py ruta/al/archivo1.pdf ruta/al/archivo2.pdf ...
"""
import os
import sys
import requests

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

URL        = os.environ.get("TEST_URL", "https://web-production-ac647.up.railway.app/analizar")
API_SECRET = os.environ.get("API_SECRET", "elige_una_clave_secreta_larga_aqui")

def probar(archivos_pdf: list):
    print(f"\nProbando con {len(archivos_pdf)} archivo(s)...")
    print(f"URL: {URL}\n")

    files  = []
    opened = []

    for ruta in archivos_pdf:
        f = open(ruta, "rb")
        opened.append(f)
        nombre = ruta.split("/")[-1].split("\\")[-1]
        files.append(("pdf", (nombre, f, "application/pdf")))
        print(f"  + {nombre}")

    try:
        response = requests.post(
            URL,
            headers={"X-API-Secret": API_SECRET},
            files=files,
            data={
                "message_id":  "prueba_local_001",
                "total_files": len(archivos_pdf)   # ← requerido por v2.0
            },
            timeout=180
        )

        print(f"\nStatus: {response.status_code}")
        data = response.json()

        if response.status_code == 200:
            print(f"\n{'='*60}")
            print(f"TIPO:       {data.get('tipo', 'N/A')}")
            print(f"DEPENDENCIA:{data.get('dependencia', 'N/A')}")
            print(f"ASUNTO:     {data.get('asunto', 'N/A')}")
            print(f"RADICADO:   {data.get('radicado', 'N/A')}")
            print(f"VENCIMIENTO:{data.get('vencimiento', 'N/A')}")
            print(f"RIESGO:     {data.get('riesgo', 'N/A')}")
            print(f"URGENTE:    {data.get('urgente', 'N/A')}")
            print(f"VEREDICTO:  {data.get('veredicto', 'N/A')}")
            print(f"ARCHIVOS:   {data.get('archivos_procesados', 'N/A')}")
            print(f"{'='*60}")
            print(f"\n--- ANÁLISIS ---\n")
            print(data.get('analisis', ''))
        elif response.status_code == 202:
            print(f"Acumulando: {data.get('recibidos')}/{data.get('esperados')} archivos")
        else:
            print(f"Error: {data.get('error')}")

    finally:
        for f in opened:
            f.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python test_local.py archivo1.pdf archivo2.pdf ...")
        sys.exit(1)
    probar(sys.argv[1:])