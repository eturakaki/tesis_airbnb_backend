"""
Test de integracion: MEP + conexion DB.
Ejecutar desde la raiz del proyecto:
    python tests/test_mep_y_db.py
"""
import logging
import sys
import os

# Para que Python encuentre src/ sin instalar el paquete
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

from src.db.database import make_engine
from src.utils.cotizacion_mep import obtener_mep, CotizacionMEPError

def main():
    print("=" * 50)
    print("TEST 1: Conexion a la base de datos")
    print("=" * 50)
    try:
        engine = make_engine()
        with engine.connect() as conn:
            result = conn.execute(__import__("sqlalchemy").text("SELECT version()"))
            version = result.fetchone()[0]
            print(f"OK: Conectado a PostgreSQL")
            print(f"    Version: {version[:60]}")
    except Exception as e:
        print(f"ERROR: No se pudo conectar a la DB: {e}")
        sys.exit(1)

    print()
    print("=" * 50)
    print("TEST 2: Cotizacion Dolar MEP")
    print("=" * 50)
    try:
        mep = obtener_mep(engine)
        print(f"OK: MEP obtenido: {mep} ARS/USD")
    except CotizacionMEPError as e:
        print(f"ERROR MEP: {e}")
        sys.exit(1)

    print()
    print("=" * 50)
    print("TEST 3: Cache (segunda llamada debe ser identica)")
    print("=" * 50)
    try:
        mep2 = obtener_mep(engine)
        assert mep == mep2, f"Cache roto: {mep} != {mep2}"
        print(f"OK: Cache funciona correctamente ({mep} == {mep2})")
    except Exception as e:
        print(f"ERROR cache: {e}")
        sys.exit(1)

    print()
    print("=" * 50)
    print("TEST 4: Verificar que MEP se guardo en fx_diaria")
    print("=" * 50)
    try:
        import sqlalchemy
        with engine.connect() as conn:
            row = conn.execute(
                sqlalchemy.text(
                    "SELECT fecha, moneda, cotizacion, fuente FROM fx_diaria ORDER BY fecha DESC LIMIT 1"
                )
            ).fetchone()
        if row:
            print(f"OK: Registro en DB -> fecha={row[0]}, moneda={row[1]}, cotizacion={row[2]}, fuente={row[3]}")
        else:
            print("ERROR: fx_diaria esta vacia, el guardado fallo silenciosamente")
            sys.exit(1)
    except Exception as e:
        print(f"ERROR al leer fx_diaria: {e}")
        sys.exit(1)

    print()
    print("=" * 50)
    print("TODOS LOS TESTS PASARON. Base lista para el scraper.")
    print("=" * 50)

if __name__ == "__main__":
    main()