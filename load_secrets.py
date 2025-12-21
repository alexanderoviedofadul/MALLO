import os
import json
import toml
import logging
from pathlib import Path
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class Secrets:
    def __init__(self):
        self._secrets = {}

    def __getitem__(self, key):
        return self._secrets.get(key)

    def __setitem__(self, key, value):
        self._secrets[key] = value

    def get(self, key, default=None):
        return self._secrets.get(key, default)

secrets = Secrets()

def load_secrets():
    logger.info("Iniciando carga de secretos")

    # Cargar variables de entorno desde .env
    load_dotenv()
    logger.info("Variables de entorno cargadas desde .env (si existe)")

    # Definir rutas de búsqueda para archivos de secretos
    search_paths = [
        '/opt/render/.streamlit',
        '/opt/render/project/src/.streamlit',
        '/app/.streamlit',
        '.',
        '.streamlit',
        '/etc/secrets',
    ]

    # Buscar y cargar secrets.toml
    for path in search_paths:
        secrets_path = Path(path) / 'secrets.toml'
        if secrets_path.is_file():
            logger.info(f"Archivo secrets.toml encontrado en {secrets_path}")
            try:
                with open(secrets_path, 'r') as f:
                    secrets._secrets.update(toml.load(f))
                break  # Si se encuentra y carga correctamente, salimos del bucle
            except Exception as e:
                logger.error(f"Error al leer {secrets_path}: {str(e)}")

    # Cargar desde /etc/secrets si existe
    etc_secrets = Path('/etc/secrets')
    if etc_secrets.is_dir():
        logger.info("Directorio /etc/secrets encontrado")
        for file_path in etc_secrets.iterdir():
            if file_path.is_file():
                logger.info(f"Procesando archivo: {file_path}")
                try:
                    with open(file_path, 'r') as f:
                        if file_path.suffix == '.json':
                            secrets._secrets.update(json.load(f))
                        elif file_path.suffix == '.toml':
                            secrets._secrets.update(toml.load(f))
                        else:
                            secrets._secrets[file_path.name] = f.read().strip()
                except Exception as e:
                    logger.error(f"Error al leer {file_path}: {str(e)}")

    # Cargar desde variables de entorno (solo si no existen en secrets.toml)
    for key, value in os.environ.items():
        if key.startswith('MALLO_'):
            # Siempre cargar variables MALLO_ (son específicas de la aplicación)
            secrets._secrets[key[6:]] = value
        elif key in ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'TOGETHER_API_KEY']:
            # Solo cargar si no existe en secrets.toml o si es un placeholder
            current_value = secrets._secrets.get(key, '')
            if key not in secrets._secrets or current_value.startswith('YOUR_') or not current_value.startswith('sk-'):
                secrets._secrets[key] = value

    logger.info(f"Carga de secretos completada. {len(secrets._secrets)} secretos cargados.")

def get_secret(key, default=None):
    """Obtiene un secreto específico."""
    return secrets.get(key, default)