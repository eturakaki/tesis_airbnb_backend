import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# 1. Cargar las credenciales ocultas desde el archivo .env
load_dotenv()

db_user = os.getenv("DB_USER")
db_password = os.getenv("DB_PASSWORD")
db_host = os.getenv("DB_HOST")
db_port = os.getenv("DB_PORT")
db_name = os.getenv("DB_NAME")

# 2. Armar la llave maestra (String de conexión)
database_url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

print("Iniciando prueba de conexión a PostgreSQL...")

try:
    # 3. Encender el motor de SQLAlchemy
    engine = create_engine(database_url)
    
    # 4. Entrar a la base y hacerle una pregunta simple
    with engine.connect() as connection:
        # Le pedimos a la base que nos diga qué versión es
        result = connection.execute(text("SELECT version();"))
        db_version = result.fetchone()[0]
        
        print("\n✅ ¡CONEXIÓN EXITOSA, HERMANO! ✅")
        print(f"Versión detectada: {db_version}")
        print("El entorno Python y PostgreSQL están comunicándose a la perfección. La cañería está lista.\n")
        
except Exception as e:
    print("\n❌ ERROR DE CONEXIÓN ❌")
    print("Algo falló al intentar entrar a la base de datos. Detalle del error:")
    print(e)