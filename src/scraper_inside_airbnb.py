import os
import re
import pandas as pd
from playwright.sync_api import sync_playwright
import time

# Configuración de rutas
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DATA_DIR = os.path.join(BASE_DIR, 'data', 'raw')
ARCHIVO_CABA = os.path.join(RAW_DATA_DIR, 'caba_listings_test.csv.gz')

def extraer_precio_con_clase(page):
    """
    Función quirúrgica para encontrar el precio en el HTML de Airbnb.
    Busca patrones como '$15.000', '$ 15.000', etc.
    """
    try:
        # Buscamos en todo el texto de la página
        contenido = page.content()
        # Buscamos el patrón de precio (Signo pesos + números + puntos)
        match = re.search(r'\$\s?(\d{1,3}(\.\d{3})*)', contenido)
        if match:
            return match.group(0) # Devuelve "$15.000"
        return "No encontrado"
    except:
        return "Error"

def ejecutar_scraper_profesional():
    print("🚀 Iniciando Motor Scraper Propio - UNSa Salta Edition")
    
    # 1. Cargamos el mapa de IDs
    df = pd.read_csv(ARCHIVO_CABA, compression='gzip', usecols=['id'])
    ids_test = df['id'].dropna().head(3).tolist() # Probamos con 3 para no quemar la IP
    
    with sync_playwright() as p:
        # Lanzamos navegador visible para que veas la magia
        browser = p.chromium.launch(headless=False) 
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        resultados = []

        for property_id in ids_test:
            url = f"https://www.airbnb.com.ar/rooms/{property_id}"
            print(f"🔎 Analizando propiedad ID: {property_id}...")
            
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                time.sleep(5) # Pausa humana para evitar baneos
                
                precio = extraer_precio_con_clase(page)
                print(f"💰 Precio detectado: {precio}")
                
                resultados.append({"id": property_id, "precio_crudo": precio})
            except Exception as e:
                print(f"❌ Error en ID {property_id}: {e}")

        browser.close()
        
        # Mostramos el resultado final
        print("\n📊 RESULTADOS DE LA PRUEBA:")
        print(pd.DataFrame(resultados))

if __name__ == "__main__":
    ejecutar_scraper_profesional()