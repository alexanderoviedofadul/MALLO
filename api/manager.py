"""Singleton del AgentManager de MALLO para la API.

AgentManager inicializa todos los clientes LLM en su constructor (incluye
llamadas de red de verificación), por eso se crea una sola vez y se reutiliza.
"""

import logging
import os
import threading
from typing import Any, Dict, Optional

import yaml

from load_secrets import load_secrets
from agents import AgentManager

logger = logging.getLogger("mallo.api.manager")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_ROOT, "config.yaml")

_manager: Optional[AgentManager] = None
_config: Optional[Dict[str, Any]] = None
_lock = threading.Lock()


def load_config() -> Dict[str, Any]:
    global _config
    if _config is None:
        with open(_CONFIG_PATH, "r") as f:
            _config = yaml.safe_load(f)
    return _config


def get_manager() -> AgentManager:
    global _manager
    if _manager is None:
        with _lock:
            if _manager is None:
                load_secrets()
                logger.info("Inicializando AgentManager (verify=False)...")
                _manager = AgentManager(load_config(), verify=False)
                logger.info(
                    "AgentManager listo. Clientes activos: %s",
                    list(_manager.clients.keys()),
                )
    return _manager
