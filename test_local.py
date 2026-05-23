"""
Script para probar el microservicio localmente.
Uso: python test_local.py ruta/al/resolucion.pdf ruta/al/soporte.pdf
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

# Configura estos valores
URL        = os.environ.get("TEST_URL", "https://web-production-ac647.up.railway.app/analizar")
API_SECRET = os.environ.get("API_SECRET", "elige_una_clave_secreta_larga_aqui")  # debe coincidir con .env

def probar(archivos_pdf: list):
    print(f"\nProbando con {len(archivos_pdf)} archivo(s)...")

    files = []
    opened = []
    for ruta in archivos_pdf:
        f = open(ruta, "rb")
        opened.append(f)
        nombre = ruta.split("/")[-1]
        files.append(("pdf", (nombre, f, "application/pdf")))

    try:
        response = requests.post(
            URL,
            headers={"X-API-Secret": API_SECRET},
            files=files,
            data={"message_id": "prueba_local"},
            timeout=120
        )

        print(f"\nStatus: {response.status_code}")
        data = response.json()

        if response.status_code == 200:
            print(f"\n{'='*50}")
            print(f"VEREDICTO: {data['veredicto']}")
            print(f"Archivos procesados: {data['archivos_procesados']}")
            print(f"\n--- ANÁLISIS ---")
            print(data['analisis'])
        else:
            print(f"Error: {data.get('error')}")

    finally:
        for f in opened:
            f.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python test_local.py archivo1.pdf archivo2.pdf ...")
        sys.exit(1)

    archivos = sys.argv[1:]
    probar(archivos)
