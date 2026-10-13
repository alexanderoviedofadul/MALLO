import requests
import streamlit as st
from typing import Tuple, Dict, Any, Optional, List
from together import Together
import asyncio
import socket
import re
from groq import Groq
import time
import logging
from openai import OpenAI
from anthropic import Anthropic
import cohere
import json
import random


# Definir función log_error localmente para evitar importación circular
def log_error(message):
    """Registra un mensaje de error en el log."""
    logging.error(message)


from load_secrets import load_secrets, get_secret, secrets

# Carga todos los secretos al inicio de la aplicación
load_secrets()

try:
    from mistralai import Mistral
except TypeError:
    print("Error al importar Mistral. Usando una implementación alternativa.")

    class Mistral:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            return "Mistral no está disponible en este momento."


# Clase de selección de agentes
class AgentSelector:
    def __init__(self, config: Dict, specialized_assistants: List[Dict]):
        self.config = config
        self.specialized_assistants = specialized_assistants
        self.default_model = config["agent_selection"]["default_model"]
        self.fallback_type = config["agent_selection"]["fallback_type"]
        self.threshold = config["agent_selection"]["threshold"]
        # Embeddings para la selección semántica de agentes. Backend preferente:
        # Cohere; si su key está ausente/comentada, se usa Mistral (mistral-embed);
        # si ninguno está disponible, se degrada a la selección de respaldo.
        cohere_key = get_secret("COHERE_API_KEY")
        self.cohere_client = cohere.Client(api_key=cohere_key) if cohere_key else None
        self._mistral_key = get_secret("MISTRAL_API_KEY")
        self._mistral_embed = None  # se crea perezosamente
        if self.cohere_client:
            self.embed_backend = "cohere"
        elif self._mistral_key:
            self.embed_backend = "mistral"
        else:
            self.embed_backend = None

    # Seleccionar el agente más adecuado para una consulta
    def select_agent(self, query: str) -> Tuple[str, float]:
        # Sin backend de embeddings -> usar el agente de respaldo.
        if not self.embed_backend:
            return self.get_fallback_agent()
        try:
            query_embedding = self.get_embedding(query)
        except Exception as e:  # noqa: BLE001
            logging.warning(f"Embeddings ({self.embed_backend}) no disponibles: {e}. Usando fallback.")
            return self.get_fallback_agent()
        best_score = 0
        best_agent = None

        for assistant in self.specialized_assistants:
            score = self.calculate_similarity(query_embedding, assistant)
            if score > best_score:
                best_score = score
                best_agent = assistant

        if best_score >= self.threshold:
            return best_agent["id"], best_score
        else:
            return self.get_fallback_agent()

    # Generar embedding del texto con el backend disponible (cohere o mistral)
    def get_embedding(self, text: str) -> List[float]:
        if self.embed_backend == "cohere":
            response = self.cohere_client.embed(
                texts=[text], model="embed-english-v3.0", input_type="search_query"
            )
            return response.embeddings[0]
        if self.embed_backend == "mistral":
            if self._mistral_embed is None:
                from mistralai import Mistral
                self._mistral_embed = Mistral(api_key=self._mistral_key)
            resp = self._mistral_embed.embeddings.create(
                model="mistral-embed", inputs=[text]
            )
            return resp.data[0].embedding
        raise RuntimeError("No hay backend de embeddings disponible")

    # Calcular la similitud entre dos vectores de incrustación
    def calculate_similarity(
        self, query_embedding: List[float], assistant: Dict
    ) -> float:
        assistant_embedding = self.get_embedding(" ".join(assistant["keywords"]))
        return self.cosine_similarity(query_embedding, assistant_embedding)

    # Calcular la similitud del coseno entre dos vectores
    def cosine_similarity(self, a: List[float], b: List[float]) -> float:
        dot_product = sum(x * y for x, y in zip(a, b))
        magnitude_a = sum(x * x for x in a) ** 0.5
        magnitude_b = sum(x * x for x in b) ** 0.5
        return dot_product / (magnitude_a * magnitude_b)

    # Obtener el agente de reserva
    def get_fallback_agent(self) -> Tuple[str, float]:
        for assistant in self.specialized_assistants:
            if assistant["name"] == self.fallback_type:
                return assistant["id"], 0.0
        return self.specialized_assistants[0]["id"], 0.0


# Clase de gestión de agentes
class AgentManager:
    def __init__(self, config: Dict[str, Any], verify: bool = True):
        # verify=False omite la verificación exhaustiva de modelos (verify_models),
        # que prueba cada modelo con una llamada real. Útil para arranques rápidos
        # (p. ej. el servicio API), donde la selección usa clients/config y el
        # fallback en tiempo de request cubre los modelos no disponibles.
        self.config = config
        self.specialized_assistants = config.get("specialized_assistants", [])
        self.agent_selector = AgentSelector(config, self.specialized_assistants)
        self.clients = {}  # Inicializa esto con tus clientes de API
        self.agent_speeds = {}
        self.ollama_models = self.get_ollama_models()
        self.openai_models = config.get("openai", {}).get("models", [])
        self.together_models = config.get("together", {}).get("models", [])
        self.specialized_assistants = config.get("specialized_assistants", [])
        self.moa_threshold = config.get("thresholds", {}).get("moa_complexity", 0.7)
        self.agent_speeds = {}
        self.available_models = {}  # Se inicializará después de los clientes
        self.reliable_models = []
        self.default_local_model = config["ollama"].get(
            "default_model", "gemma3:latest"
        )
        self.processing_priority = config.get("processing_priority", [])
        # Agente por defecto: el default_model de Ollama Cloud; respaldo: el primero
        # de la cascada `fallback_agents` (config.yaml). Antes estaban fijados aquí
        # (qwen3-coder:480b, retirado, y DeepSeek, cuya clave ya no vale).
        self.default_agent = ("ollama_cloud", config["ollama_cloud"]["default_model"])
        self.fallback_agents = [
            tuple(str(a).split(":", 1)) for a in config.get("fallback_agents", []) or []
            if ":" in str(a)
        ]
        self.backup_default_agent = (
            self.fallback_agents[0] if self.fallback_agents
            else ("mistral", config["mistral"]["default_model"])
        )
        self.meta_analysis_model = config["evaluation_models"]["meta_analysis"]["model"]
        self.meta_analysis_api = config["evaluation_models"]["meta_analysis"]["api"]

        # Añadir estas líneas
        self.critical_analysis_config = config.get("critical_analysis", {})
        self.critical_analysis_probability = self.critical_analysis_config.get(
            "probability", 0.2
        )
        self.critical_analysis_prompts = self.critical_analysis_config.get(
            "prompts", {}
        )

        # Mapa asistente especializado -> área del corpus jurídico (cedula360
        # /api/legal). Reemplaza a los OpenAI Assistants: los expertos se sirven
        # desde claude-for-legal-colombia vía /api/legal/ask, sin depender de OpenAI.
        self.assistant_area_map = self._load_assistant_area_map()

        # Inicializar los clientes de API. Los proveedores de `disabled_providers`
        # (config.yaml) NO se inicializan ni se comprueban: su clave puede seguir en
        # secrets.toml, pero el arranque no los intenta (2026-10-04: solo quedan
        # activas las APIs que respondieron a una llamada real).
        self.clients = {}
        self.disabled_providers = set(config.get("disabled_providers", []) or [])
        inicializadores = [
            ("openai", self.init_openai_client),
            ("together", self.init_together_client),
            ("groq", self.init_groq_client),
            ("deepinfra", self.init_deepinfra_client),
            ("anthropic", self.init_anthropic_client),
            ("deepseek", self.init_deepseek_client),
            ("mistral", self.init_mistral_client),
            ("cohere", self.init_cohere_client),
            ("openrouter", self.init_openrouter_client),
            ("z-ai", self.init_z_ai_client),
            ("routellm", self.init_routellm_client),
            ("ollama_cloud", self.init_ollama_cloud_client),
            ("minimax", self.init_minimax_client),
        ]
        for nombre, iniciar in inicializadores:
            if nombre in self.disabled_providers:
                logging.info(f"{nombre}: desactivado en config.yaml (disabled_providers); no se inicializa")
                continue
            cliente = iniciar()
            if cliente:
                self.clients[nombre] = cliente
                if nombre == "openai":
                    self.clients["api"] = cliente  # Alias para compatibilidad

        # Verificar modelos disponibles después de inicializar todos los clientes
        self.available_models = self.verify_models() if verify else {}

    def get_specialized_assistant(self, assistant_id: str) -> Optional[Dict[str, Any]]:
        """
        Obtiene un asistente especializado por su ID.

        Args:
        assistant_id (str): El ID del asistente especializado.

        Returns:
        Optional[Dict[str, Any]]: Un diccionario con la información del asistente especializado,
                                o None si no se encuentra.
        """
        for assistant in self.specialized_assistants:
            if assistant["id"] == assistant_id:
                return assistant
        return None

    def get_general_agents(
        self, query: str, complexity: float, prompt_type: str
    ) -> List[Tuple[str, str, str]]:
        general_agents = []
        for api, config in self.config.items():
            # Solo proveedores con cliente activo (la sección "ollama" no es un
            # agent_type despachable: el local se usa como último recurso).
            if api == "ollama" or not self.proveedor_disponible(api):
                continue
            if isinstance(config, dict) and "models" in config:
                for model in config["models"]:
                    if (
                        isinstance(model, dict)
                        and "specialty" in model
                        and "prompt_types" in model
                    ):
                        if (
                            model["specialty"] == "general"
                            or prompt_type in model["prompt_types"]
                        ):
                            general_agents.append(
                                (
                                    api,
                                    model["name"],
                                    f"{api.capitalize()} {model['name']}",
                                )
                            )

        # Ordenar los agentes generales por relevancia (puedes implementar tu propia lógica de ordenación aquí)
        general_agents.sort(
            key=lambda x: self.calculate_relevance(x, query, prompt_type), reverse=True
        )

        return general_agents

    def calculate_relevance(
        self, agent: Tuple[str, str, str], query: str, prompt_type: str
    ) -> float:
        # Esta es una función de ejemplo. Deberías adaptarla según tus necesidades específicas.
        agent_type, model, _ = agent
        relevance = 0.0

        # Dar mayor relevancia a ciertos tipos de agentes según el prompt_type
        if prompt_type == "math" and agent_type in ["deepinfra"]:
            relevance += 0.5
        elif prompt_type == "coding" and agent_type in [
            "deepinfra",
            "deepseek",
            "mistral",
        ]:
            relevance += 0.5

        # Considerar la complejidad del modelo (esto es un ejemplo, ajusta según tus modelos)
        if "large" in model.lower() or "advanced" in model.lower():
            relevance += 0.3

        # Añadir un poco de aleatoriedad para diversidad
        relevance += random.random() * 0.2

        return relevance

    # Actualizar los modelos disponibles
    def get_appropriate_agent(self, query: str, complexity: float) -> Tuple[str, str]:
        agent_id, score = self.agent_selector.select_agent(query)
        selected_agent = next(
            (a for a in self.specialized_assistants if a["id"] == agent_id), None
        )

        if selected_agent:
            validated_agent = self.validate_agent_selection(
                query, selected_agent["name"]
            )
            if validated_agent != selected_agent["name"]:
                agent_id = next(
                    (
                        a["id"]
                        for a in self.specialized_assistants
                        if a["name"] == validated_agent
                    ),
                    agent_id,
                )

        return "assistant", agent_id

    # Validar la selección del agente
    def validate_agent_selection(self, query: str, initial_agent: str) -> str:
        validation_prompt = f"""
        Analiza la siguiente consulta y determina si el agente seleccionado es el más apropiado.

        Consulta: {query}

        Agente inicial seleccionado: {initial_agent}

        Agentes disponibles:
        {', '.join([assistant['name'] for assistant in self.specialized_assistants])}

        Proporciona tu recomendación en el siguiente formato:
        Agente recomendado: [nombre del agente]
        Confianza: [alta/media/baja]
        Justificación: [tu justificación]
        """

        # Antes: Cohere fijo (clients["cohere"].generate), que no está activo.
        # Ahora: "api:modelo" de agent_selection.validation_model (config.yaml).
        api, _, modelo = str(self.config["agent_selection"]["validation_model"]).partition(":")
        try:
            texto = self.process_query(validation_prompt, api, modelo)
        except Exception as e:  # noqa: BLE001
            logging.warning(f"Validación de agente no disponible ({e}); se mantiene {initial_agent}")
            return initial_agent

        recommended_agent, confidence = self.extract_recommendation(texto)

        return recommended_agent if confidence == "alta" else initial_agent

    # Extraer la recomendación y la confianza de la respuesta generada
    @staticmethod
    def extract_recommendation_(response: str) -> Tuple[str, str]:
        agent_match = re.search(r"Agente recomendado:\s*(\w+)", response)
        recommended_agent = agent_match.group(1) if agent_match else None

        confidence_match = re.search(
            r"Confianza:\s*(alta|media|baja)", response, re.IGNORECASE
        )
        confidence = confidence_match.group(1).lower() if confidence_match else "baja"

        return recommended_agent, confidence

    # Clase de gestión de agentes especializados donde se selecciona el agente más adecuado para una consulta
    def select_specialized_agent(self, query: str) -> Optional[Dict[str, str]]:
        best_score = 0
        best_agent = None
        query_lower = query.lower()

        for agent in self.specialized_assistants:
            score = sum(
                1
                for keyword in agent["keywords"]
                if re.search(r"\b" + re.escape(keyword.lower()) + r"\b", query_lower)
            )
            if score > best_score:
                best_score = score
                best_agent = agent

        if best_agent:
            return {
                "type": "assistant",  # Usar "assistant" para procesar con OpenAI Assistants API
                "id": best_agent["id"],
                "name": best_agent["name"],
            }

        return None

    # Procesar la consulta con el agente seleccionado
    def apply_critical_analysis_prompt(self, query: str) -> str:
        if random.random() < self.critical_analysis_probability:
            prompt_type = self.determine_prompt_type(query)
            prompt = self.critical_analysis_prompts.get(
                prompt_type, self.critical_analysis_prompts["default"]
            )
            return f"{prompt}\n\n{query}"
        return query

    # Determinar el tipo de prompt crítico a utilizar
    def get_available_models(self, agent_type: str = None) -> List[str]:
        if agent_type:
            models = {
                "api": self.openai_models,
                "groq": self.config["groq"]["models"],
                "together": self.together_models,
                "local": self.ollama_models,
                "openrouter": self.config["openrouter"]["models"],
                "deepinfra": self.config["deepinfra"]["models"],
                "anthropic": self.config["anthropic"]["models"],
                "deepseek": self.config["deepseek"]["models"],
                "mistral": self.config["mistral"]["models"],
                "cohere": self.config["cohere"]["models"],
                "z-ai": self.config["z-ai"]["models"],
                "routellm": self.config["routellm"]["models"],
            }.get(agent_type, [])

            # Procesar los modelos para manejar diferentes formatos
            processed_models = []
            for model in models:
                if isinstance(model, dict) and "name" in model:
                    processed_models.append(model["name"])
                elif isinstance(model, str):
                    processed_models.append(model)
            return processed_models
        else:
            all_agents = []
            for agent_type in [
                "groq",
                "deepseek",
                "openrouter",
                "together",
                "deepinfra",
                "anthropic",
                "mistral",
                "cohere",
                "api",
                "local",
                "z-ai",
                "routellm",
            ]:
                all_agents.extend(self.get_available_models(agent_type))
            return all_agents

    # Valida el tipo de prompt crítico a utilizar
    def validate_prompt_type(self, query: str, initial_type: str) -> str:
        config = self.config["prompt_selection"]

        # Usar el modelo especificado para validar el tipo de prompt
        validation_prompt = f"""
        Analiza la siguiente consulta y determina el tipo más apropiado de prompt a utilizar.
        Tipos disponibles: {', '.join(self.critical_analysis_prompts.keys())}

        Consulta: {query}

        Tipo inicial sugerido: {initial_type}

        Proporciona tu recomendación en el siguiente formato:
        Tipo recomendado: [tipo]
        Confianza: [alta/media/baja]
        Justificación: [tu justificación]
        """

        # "api:modelo" — se parte solo en el primer ":" (los nombres de Ollama
        # llevan ":" propio, p. ej. ollama_cloud:gpt-oss:20b).
        api, _, modelo = str(config["default_model"]).partition(":")
        try:
            response = self.process_query(validation_prompt, api, modelo)
        except Exception as e:  # noqa: BLE001
            logging.warning(f"Validación de tipo de prompt no disponible ({e}); se mantiene {initial_type}")
            return initial_type

        # Extraer el tipo recomendado y la confianza de la respuesta
        recommended_type, confidence = self.extract_recommendation(response)

        # Si la confianza es alta, usar el tipo recomendado; de lo contrario, mantener el inicial
        if confidence == "alta":
            return recommended_type
        else:
            return initial_type

    # Extraer la recomendación y la confianza de la respuesta generada
    def extract_recommendation(self, response: str) -> Tuple[str, str]:
        # Varios modelos contestan en Markdown («**Tipo recomendado:** legal»): sin
        # quitar los asteriscos el patrón no casaba y se perdía la recomendación.
        response = (response or "").replace("*", "").replace("`", "")
        # Extraer el tipo recomendado
        type_match = re.search(r"Tipo recomendado:\s*(\w+)", response)
        recommended_type = type_match.group(1) if type_match else "default"

        # Extraer la confianza
        confidence_match = re.search(
            r"Confianza:\s*(alta|media|baja)", response, re.IGNORECASE
        )
        confidence = confidence_match.group(1).lower() if confidence_match else "baja"

        return recommended_type, confidence

    # Determinar el tipo de prompt crítico a utilizar
    def get_prioritized_agents(
        self, query: str, complexity: float, prompt_type: str
    ) -> List[Tuple[str, str, str]]:
        prioritized_agents = []
        used_agent_types = set()
        used_models = set()

        # Función auxiliar para encontrar modelos especializados
        def find_specialized_models(prompt_type):
            specialized_models = []
            for api, config in self.config.items():
                if api == "ollama" or not self.proveedor_disponible(api):
                    continue
                if isinstance(config, dict) and "models" in config:
                    for model in config["models"]:
                        if (
                            isinstance(model, dict)
                            and "specialty" in model
                            and "prompt_types" in model
                        ):
                            if (
                                model["specialty"] == prompt_type
                                or prompt_type in model["prompt_types"]
                            ):
                                specialized_models.append(
                                    (
                                        api,
                                        model["name"],
                                        f"{api.capitalize()} {model['name']}",
                                    )
                                )
            return specialized_models

        # 1) Modelo elegido por tarea (`task_models` en config.yaml, medido con
        #    prompts reales de MALLO; ver docs/MODELOS-OLLAMA-CLOUD-POR-FLUJO-2026-10-04.md).
        #    Se toma el primero disponible de la cascada del tipo de consulta.
        task_models = self.config.get("task_models", {}) or {}
        por_tarea = False
        # Tipos sin cascada propia (incluido "default", que es lo que devuelve el
        # clasificador por palabras clave casi siempre): primero el experto
        # jurídico si las palabras clave lo señalan, luego la cascada general.
        cascada = task_models.get(prompt_type) or (["assistant:auto"] + list(task_models.get("general") or []))
        for entrada in cascada:
            if len(prioritized_agents) >= 3:
                break
            agent_type, _, model = str(entrada).partition(":")
            if agent_type in used_agent_types:
                continue  # un agente por proveedor, como en el resto de la selección
            if agent_type == "assistant" and model == "auto":
                # Experto jurídico (cedula360 /api/legal) elegido por palabras clave.
                experto = self.select_specialized_agent(query) if self.proveedor_disponible("assistant") else None
                if experto:
                    prioritized_agents.append(("assistant", experto["id"], experto["name"]))
                    used_agent_types.add("assistant")
                continue
            if model and self.proveedor_disponible(agent_type):
                prioritized_agents.append(
                    (agent_type, model, f"{agent_type.capitalize()} {model} ({prompt_type})")
                )
                used_agent_types.add(agent_type)
                used_models.add(model)
                por_tarea = True

        # 2) Si no hay modelo por tarea, el primero de config.yaml que declare ese prompt_type
        specialized_models = find_specialized_models(prompt_type) if not prioritized_agents else []
        for agent_type, model, name in specialized_models:
            if agent_type not in used_agent_types and model not in used_models:
                prioritized_agents.append((agent_type, model, name))
                used_agent_types.add(agent_type)
                used_models.add(model)
                break

        # Si no se encontró un modelo especializado, intentar seleccionar un agente especializado
        if not prioritized_agents:
            specialized_agent = self.select_specialized_agent(query)
            if specialized_agent:
                # agent_type "assistant" para que process_query lo despache a
                # process_with_assistant (→ experto vía cedula360 /api/legal).
                prioritized_agents.append(
                    ("assistant", specialized_agent["id"], specialized_agent["name"])
                )
                used_agent_types.add("assistant")

        # Añadir agentes generales hasta tener un total de 3 agentes (solo si no hay
        # cascada por tarea: su orden es aleatorio y desplazaría a la elegida).
        general_agents = [] if por_tarea else self.get_general_agents(query, complexity, prompt_type)
        for agent in general_agents:
            if len(prioritized_agents) >= 3:
                break
            agent_type, model, name = agent
            if agent_type not in used_agent_types and model not in used_models:
                prioritized_agents.append(agent)
                used_agent_types.add(agent_type)
                used_models.add(model)

        # Si aún no tenemos 3 agentes, añadir agentes por defecto (Ollama Cloud
        # como motor principal y la cascada `fallback_agents` como respaldo).
        default_agents = [
            ("ollama_cloud", self.config["ollama_cloud"]["default_model"], "Ollama Cloud Default"),
        ] + [(a, m, f"{a.capitalize()} {m} (respaldo)") for a, m in self.fallback_agents]
        for agent_type, model, name in default_agents:
            if len(prioritized_agents) >= 3:
                break
            if not self.proveedor_disponible(agent_type) or agent_type == "local":
                continue
            if agent_type not in used_agent_types and model not in used_models:
                prioritized_agents.append((agent_type, model, name))
                used_agent_types.add(agent_type)
                used_models.add(model)

        # Reordenar respetando processing_priority (ollama_cloud primero), de forma
        # estable: los agentes cuyo proveedor está antes en la prioridad van primero.
        priority = self.config.get("processing_priority", [])

        def _prio(agent):
            at = agent[0]
            try:
                return priority.index(at)
            except ValueError:
                return len(priority) + 1

        prioritized_agents.sort(key=_prio)
        return prioritized_agents[:3]

    def is_suitable_for_prompt_type(
        self, agent_type: str, model_id: str, prompt_type: str
    ) -> bool:
        # Implementa la lógica para determinar si un agente/modelo es adecuado para un tipo de prompt específico
        # Por ahora, simplemente retornamos True
        return True

    def find_suitable_assistant(self, query: str) -> Optional[str]:
        best_score = 0
        best_assistant_id = None
        query_lower = query.lower()

        for assistant in self.specialized_assistants:
            score = sum(
                1 for keyword in assistant["keywords"] if keyword.lower() in query_lower
            )
            if score > best_score:
                best_score = score
                best_assistant_id = assistant["id"]

        return best_assistant_id

    # Procesar la consulta con el agente seleccionado y el tipo de prompt crítico
    def process_query_with_fallback(
        self, query: str, prioritized_agents: List[Tuple[str, str]]
    ) -> Tuple[str, Dict[str, Any]]:
        for agent_type, agent_id in prioritized_agents:
            # Manejar el caso en que agent_id sea un diccionario
            if isinstance(agent_id, dict):
                if "name" in agent_id:
                    agent_id = agent_id["name"]
                else:
                    logging.warning(f"Formato de modelo incorrecto: {agent_id}")
                    continue

            # Verificar si el cliente está inicializado
            if agent_type not in ["local", "assistant"] and not self.clients.get(
                agent_type
            ):
                logging.warning(f"Cliente {agent_type} no inicializado, omitiendo")
                continue

            try:
                response = self.process_query(query, agent_type, agent_id)
                return response, {"agent": agent_type, "model": agent_id}
            except Exception as e:
                logging.error(
                    f"Error processing query with {agent_type}:{agent_id}: {str(e)}"
                )

        # Si todos los agentes fallan, usar el modelo local por defecto
        try:
            response = self.process_with_local_model(query, self.default_local_model)
            return response, {"agent": "local", "model": self.default_local_model}
        except Exception as e:
            logging.error(f"Error processing query with local model: {str(e)}")
            raise ValueError(
                "No se pudo procesar la consulta con ningún agente disponible"
            )

    # Procesar la consulta con el agente seleccionado
    def init_client(self, client_name: str, client_class, api_key: str, **kwargs):
        # Sin API key (p. ej. comentada en secrets.toml) -> no se inicializa el
        # cliente, por lo que el proveedor queda como NO disponible.
        if not api_key:
            logging.info(f"{client_name}: sin API key, cliente no inicializado")
            return None
        try:
            # Debug: check kwargs
            if 'proxies' in kwargs:
                logging.warning(f"init_client for {client_name} received 'proxies' in kwargs")

            return client_class(api_key=api_key, **kwargs)
        except TypeError as e:
            if "proxies" in str(e):
                 logging.warning(f"Error initializing {client_name}: {e}. Retrying without 'proxies' if present.")
                 if 'proxies' in kwargs:
                     kwargs.pop('proxies')
                     try:
                         return client_class(api_key=api_key, **kwargs)
                     except Exception as e2:
                         logging.error(f"Retry failed for {client_name}: {e2}")
            logging.warning(f"Error al inicializar {client_name} client: {str(e)}")
            return None
        except Exception as e:
            logging.warning(f"Error al inicializar {client_name} client: {str(e)}")
            return None

    # Inicializar el cliente de OpenAI
    def init_openai_client(self):
        client = self.init_client("OpenAI", OpenAI, get_secret("OPENAI_API_KEY"))
        if client:
            # Verificar que el cliente funciona
            try:
                client.models.list()
                return client
            except Exception as e:
                logging.error(f"Error checking OpenAI API: {str(e)}")
        return None

    # Inicializar el cliente de Together
    def init_together_client(self):
        return self.init_client("Together", Together, get_secret("TOGETHER_API_KEY"))

    # Inicializar el cliente de Groq
    def init_groq_client(self):
        client = self.init_client("Groq", Groq, get_secret("GROQ_API_KEY"))
        if client:
            try:
                # Usar un modelo que sabemos que existe en Groq
                client.chat.completions.create(
                    model="llama-3.1-8b-instant",  # Modelo disponible en Groq
                    messages=[{"role": "user", "content": "Test"}],
                    max_tokens=5,  # Usar pocos tokens para la prueba
                )
                return client
            except Exception as e:
                logging.error(f"Error checking Groq API: {str(e)}")
        return None

    # Inicializar el cliente de DeepInfra
    def init_deepinfra_client(self):
        return self.init_client(
            "DeepInfra",
            OpenAI,
            get_secret("DEEPINFRA_API_KEY"),
            base_url="https://api.deepinfra.com/v1/openai",
        )

    # Inicializar el cliente de Anthropic
    def init_anthropic_client(self):
        return self.init_client("Anthropic", Anthropic, get_secret("ANTHROPIC_API_KEY"))

    # Inicializar el cliente de DeepSeek
    def init_deepseek_client(self):
        return self.init_client(
            "DeepSeek",
            OpenAI,
            get_secret("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com/v1",
        )

    # Inicializar el cliente de Mistral
    def init_mistral_client(self):
        return self.init_client("Mistral", Mistral, get_secret("MISTRAL_API_KEY"))

    # Inicializar el cliente de Cohere
    def init_cohere_client(self):
        return self.init_client("Cohere", cohere.Client, get_secret("COHERE_API_KEY"))

    # Inicializar el cliente de OpenRouter
    def init_openrouter_client(self):
        client = self.init_client(
            "OpenRouter",
            OpenAI,
            get_secret("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
        )
        if client:
            try:
                # Probar el cliente con un modelo gratuito verificado
                client.chat.completions.create(
                    model="google/gemma-4-31b-it:free",
                    messages=[{"role": "user", "content": "Test"}],
                    max_tokens=20,  # Mínimo 16 requerido por algunos backends (Azure)
                )
                return client
            except Exception as e:
                logging.error(f"Error checking OpenRouter API: {str(e)}")
        return None

    # Inicializar el cliente de Z.AI
    def init_z_ai_client(self):
        client = self.init_client(
            "Z.AI",
            OpenAI,
            get_secret("ZAI_API_KEY"),
            base_url="https://api.z.ai/api/paas/v4/",
        )
        if client:
            try:
                # Probar el cliente con el modelo GLM-4.6
                client.chat.completions.create(
                    model="glm-4.6",
                    messages=[{"role": "user", "content": "Test"}],
                    max_tokens=20,  # Mínimo 16 requerido por algunos backends
                )
                return client
            except Exception as e:
                logging.error(f"Error checking Z.AI API: {str(e)}")
        return None

    # Inicializar el cliente de RouteLLM
    def init_routellm_client(self):
        client = self.init_client(
            "RouteLLM",
            OpenAI,
            get_secret("ROUTELLM_API_KEY"),
            base_url="https://routellm.abacus.ai/v1",
        )
        if client:
            try:
                # Probar el cliente con el modelo route-llm
                client.chat.completions.create(
                    model="route-llm",
                    messages=[{"role": "user", "content": "Test"}],
                    max_tokens=20,  # Mínimo 16 requerido por algunos backends
                )
                return client
            except Exception as e:
                logging.error(f"Error checking RouteLLM API: {str(e)}")
        return None

    # Inicializar el cliente de Ollama Cloud (OpenAI-compatible)
    def init_ollama_cloud_client(self):
        base_url = self.config.get("ollama_cloud", {}).get(
            "base_url", "https://ollama.com/v1"
        )
        client = self.init_client(
            "Ollama Cloud",
            OpenAI,
            get_secret("OLLAMA_CLOUD_API_KEY"),
            base_url=base_url,
        )
        if client:
            try:
                # Probar el cliente con el modelo rápido por defecto
                test_model = self.config.get("ollama_cloud", {}).get(
                    "fast_model", "gpt-oss:20b"
                )
                client.chat.completions.create(
                    model=test_model,
                    messages=[{"role": "user", "content": "Test"}],
                    max_tokens=20,
                )
                return client
            except Exception as e:
                logging.error(f"Error checking Ollama Cloud API: {str(e)}")
        return None

    # Inicializar el cliente de MiniMax (vía la API de cedula360)
    def init_minimax_client(self):
        # MiniMax multimodal se consume a través de cedula360 (/api/minimax/*),
        # que habla con MiniMax por loopback. Requiere plan Enterprise/Despachos.
        api_key = get_secret("CEDULA360_API_KEY")
        if not api_key:
            return None
        base = (get_secret("CEDULA360_API_BASE") or "https://cedula360.tech/api").rstrip("/")
        try:
            r = requests.get(
                f"{base}/minimax/usage",
                headers={"X-API-Key": api_key},
                timeout=15,
            )
            if r.status_code == 200:
                logging.info(f"MiniMax (cedula360) disponible: {r.json()}")
                return {"base": base, "key": api_key}
            logging.warning(
                f"MiniMax (cedula360) no disponible: HTTP {r.status_code} "
                f"(¿plan sin acceso?)"
            )
        except Exception as e:
            logging.error(f"Error checking MiniMax (cedula360): {str(e)}")
        return None

    def process_with_minimax(self, query: str, model: str = None) -> str:
        try:
            client = self.clients.get("minimax")
            if not client:
                return {"error": "Cliente de MiniMax (cedula360) no inicializado"}
            payload = {"message": query}
            if model and model != "MiniMax-M3":
                payload["model"] = model
            r = requests.post(
                f"{client['base']}/minimax/chat",
                headers={"X-API-Key": client["key"], "Content-Type": "application/json"},
                json=payload,
                timeout=120,
            )
            if r.status_code == 403:
                return "Error: MiniMax requiere plan Enterprise/Despachos en cedula360"
            if r.status_code != 200:
                return f"Error al procesar con MiniMax: HTTP {r.status_code}"
            data = r.json()
            text = (data.get("stdout") or "").strip()
            if not text:
                err = data.get("provider_error") or data.get("stderr")
                return f"Error de MiniMax: {err}" if err else "Error: respuesta vacía de MiniMax"
            return text
        except Exception as e:
            logging.error(f"Error al procesar con MiniMax (cedula360): {str(e)}")
            return f"Error al procesar con MiniMax: {str(e)}"

    # Procesar la consulta con el agente seleccionado
    def get_ollama_models(self) -> List[str]:
        try:
            response = requests.get(
                f"{self.config['ollama']['base_url'].rstrip('/')}/api/tags", timeout=5
            )
            if response.status_code == 200:
                return [model["name"] for model in response.json()["models"]]
            return []
        except requests.RequestException:
            logging.warning("No se pudieron obtener los modelos de Ollama")
            return []

    # Verificar la disponibilidad de los modelos
    def verify_models(self) -> Dict[str, List[str]]:
        available_models = {"local": self.ollama_models}
        for agent_type in [
            "api",
            "groq",
            "together",
            "openrouter",
            "deepinfra",
            "anthropic",
            "deepseek",
            "mistral",
            "cohere",
        ]:
            available_models[agent_type] = []
            models = self.get_available_agents(agent_type)
            for model in models:
                if isinstance(model, dict) and "name" in model:
                    model_name = model["name"]
                    if self.test_model_availability(agent_type, model_name):
                        available_models[agent_type].append(model_name)
                elif isinstance(model, str):
                    if self.test_model_availability(agent_type, model):
                        available_models[agent_type].append(model)
        return available_models

    # Probar la disponibilidad de un modelo
    def test_model_availability(self, agent_type: str, model: str) -> bool:
        if agent_type == "local":
            return True

        # Verificar si el cliente está inicializado
        if not self.clients.get(agent_type):
            logging.warning(f"Cliente {agent_type} no inicializado")
            return False

        # Para modelos de audio o visión, no intentar probarlos con texto
        if any(
            keyword in model.lower()
            for keyword in ["whisper", "tts", "vision", "audio"]
        ):
            logging.info(
                f"Modelo {agent_type}:{model} es un modelo de audio/visión, no se prueba con texto"
            )
            return False

        # Para modelos de OpenRouter, verificar que tengan el formato correcto
        if agent_type == "openrouter" and "/" not in model:
            logging.warning(
                f"Modelo de OpenRouter {model} no tiene el formato correcto (proveedor/modelo)"
            )
            return False

        try:
            # Usar una consulta muy corta para la prueba
            self.process_query("Test", agent_type, model)
            return True
        except Exception as e:
            logging.warning(f"Modelo {agent_type}:{model} no está disponible: {str(e)}")
            return False

    # Actualizar los modelos fiables
    def update_reliable_models(self, speed_test_results: Dict[str, float]):
        self.reliable_models = sorted(
            [
                model
                for model in speed_test_results.keys()
                if model.split(":")[1] in self.available_models[model.split(":")[0]]
            ],
            key=lambda x: speed_test_results[x],
        )
        self.reliable_models.insert(0, f"local:{self.default_local_model}")

    # Obtener el agente de reserva
    def get_appropriate_agent_(self, query: str, complexity: float) -> Tuple[str, str]:
        scores = self.calculate_agent_scores(query, complexity)

        for assistant in self.specialized_assistants:
            if any(
                keyword.lower() in query.lower() for keyword in assistant["keywords"]
            ):
                return "assistant", assistant["id"]

        if complexity < self.config["thresholds"]["local_complexity"]:
            return "local", self.default_local_model

        for model in self.reliable_models:
            agent_type, model_name = model.split(":")
            if self.is_suitable(agent_type, model_name, complexity):
                return agent_type, model_name

        return "local", self.default_local_model

    # Calcular los puntajes de los agentes
    def calculate_agent_scores(self, query: str, complexity: float) -> Dict[str, float]:
        scores = {}

        for assistant in self.specialized_assistants:
            relevance = sum(
                keyword.lower() in query.lower() for keyword in assistant["keywords"]
            )
            scores[f'assistant:{assistant["id"]}'] = relevance * 2

        scores[f"local:{self.default_local_model}"] = 1 - complexity

        if self.internet_available():
            for model in self.openai_models:
                scores[f"api:{model}"] = complexity

            for model in self.together_models:
                scores[f"together:{model}"] = complexity * 0.9

            for model in self.config["groq"]["models"]:
                scores[f"groq:{model}"] = complexity * 0.95

        for agent, speed in self.agent_speeds.items():
            if agent in scores:
                scores[agent] *= 1 / speed

        return scores

    # Validar la selección del agente
    def is_suitable(self, agent_type: str, model: str, complexity: float) -> bool:
        thresholds = self.config["thresholds"]
        if agent_type == "deepinfra" and complexity < thresholds["api_complexity"]:
            return True
        if agent_type == "openrouter" and complexity < thresholds["local_complexity"]:
            return True
        if agent_type == "deepseek" and complexity < thresholds["api_complexity"]:
            return True
        if agent_type == "local" and complexity < thresholds["local_complexity"]:
            return True
        if agent_type == "api" and complexity > thresholds["api_complexity"]:
            return True
        if (
            agent_type == "groq"
            and thresholds["local_complexity"]
            <= complexity
            <= thresholds["api_complexity"]
        ):
            return True
        if agent_type == "together" and complexity < thresholds["local_complexity"]:
            return True
        if agent_type == "anthropic" and complexity < thresholds["api_complexity"]:
            return True
        if agent_type == "cohere" and complexity < thresholds["api_complexity"]:
            return True
        if agent_type == "mistral" and complexity < thresholds["api_complexity"]:
            return True
        return False

    # Procesar la consulta con el agente seleccionado
    def process_query(
        self,
        query: str,
        agent_type: str,
        agent_id: str,
        prompt_type: str = None,
        fallback_attempts: int = 0,
        _probados=None,
    ) -> str:
        start_time = time.time()
        _probados = set(_probados or set())
        max_fallback_attempts = (
            3  # Número máximo de intentos de fallback antes de abortar
        )

        try:
            # Manejar el caso en que agent_id sea un diccionario
            if isinstance(agent_id, dict):
                if "name" in agent_id:
                    agent_id = agent_id["name"]
                else:
                    raise ValueError(f"Formato de modelo incorrecto: {agent_id}")

            # Aplicar el prompt especializado si se proporciona
            if prompt_type and prompt_type in self.critical_analysis_prompts:
                specialized_prompt = self.critical_analysis_prompts[prompt_type]
                query = f"{specialized_prompt}\n\n{query}"

            # Procesar la consulta con el agente seleccionado
            if agent_type == "assistant":
                response = self.process_with_assistant(agent_id, query)
            elif agent_type == "openai":
                response = self.process_with_api(query, agent_id)
            else:
                process_method = getattr(self, f"process_with_{agent_type}", None)
                if process_method:
                    response = process_method(query, agent_id)
                else:
                    raise ValueError(
                        f"No se pudo procesar la consulta con el agente seleccionado: {agent_type}"
                    )

            # Verificar y manejar la respuesta
            if isinstance(response, dict):
                if "error" in response:
                    raise ValueError(f"Error en la respuesta: {response['error']}")
                elif "content" in response:
                    response = response["content"]
                else:
                    raise ValueError(
                        f"Respuesta inesperada del agente {agent_type}:{agent_id}"
                    )

            # Validar la respuesta
            if not response or (
                isinstance(response, str)
                and (
                    response.strip() == ""
                    or response.startswith("Error")
                    or response.startswith("No se pudo procesar")
                )
            ):
                raise ValueError(
                    f"Respuesta inválida del agente {agent_type}:{agent_id}"
                )

            # Registrar el tiempo de procesamiento
            processing_time = time.time() - start_time
            self.agent_speeds[f"{agent_type}:{agent_id}"] = processing_time
            logging.info(
                f"Query processed by {agent_type}:{agent_id} in {processing_time:.2f} seconds"
            )

            return response

        except Exception as e:
            logging.error(
                f"Error processing query with {agent_type}:{agent_id}: {str(e)}"
            )

            # Intentar con agente de respaldo si no se ha alcanzado el límite de intentos
            if fallback_attempts < max_fallback_attempts:
                _probados.add((agent_type, agent_id if isinstance(agent_id, str) else str(agent_id)))
                fallback_agent, fallback_model = self.get_fallback_agent(_probados)
                if (fallback_agent, fallback_model) not in _probados:
                    logging.info(
                        f"Attempting fallback with {fallback_agent}:{fallback_model}"
                    )
                    # El prompt especializado ya va dentro de `query`: no se reaplica.
                    return self.process_query(
                        query,
                        fallback_agent,
                        fallback_model,
                        None,
                        fallback_attempts + 1,
                        _probados,
                    )
                else:
                    logging.error(
                        "Fallback agent is the same as the failed agent. Aborting fallback."
                    )
            else:
                logging.error(
                    f"Reached maximum fallback attempts ({max_fallback_attempts}). Unable to process query."
                )

            # Si todos los intentos fallan, lanzar una excepción con un mensaje más informativo
            raise ValueError(
                f"Failed to process query after {fallback_attempts} fallback attempts. Last error: {str(e)}"
            )

    def apply_specialized_prompt(self, query: str, prompt_type: str) -> str:
        prompt = self.critical_analysis_prompts.get(
            prompt_type, self.critical_analysis_prompts["default"]
        )
        return f"{prompt}\n\n{query}"

    def should_use_moa(self, query: str, complexity: float) -> bool:
        return complexity > self.moa_threshold

    def proveedor_disponible(self, agent_type: str) -> bool:
        """True si el proveedor puede atender ahora: cliente inicializado (y no
        desactivado), el experto vía cedula360 ("assistant") o el Ollama local."""
        if agent_type in self.disabled_providers:
            return False
        if agent_type == "assistant":
            return bool(get_secret("CEDULA360_API_KEY"))
        if agent_type in ("local", "ollama"):
            return "ollama" not in self.disabled_providers
        if agent_type == "openai":
            return bool(self.clients.get("api") or self.clients.get("openai"))
        return bool(self.clients.get(agent_type))

    def get_fallback_agent(self, exclude=None) -> Tuple[str, str]:
        """Siguiente agente de la cascada `fallback_agents` (config.yaml) que esté
        disponible y no se haya probado ya. Último recurso: el modelo local."""
        exclude = set(exclude or [])
        for agent_type, model in self.fallback_agents:
            if (agent_type, model) in exclude or not self.proveedor_disponible(agent_type):
                continue
            return agent_type, model
        return "local", self.config["ollama"]["default_model"]

    def get_next_reliable_model(self, current_model: str) -> Optional[str]:
        try:
            current_index = self.reliable_models.index(current_model)
            if current_index < len(self.reliable_models) - 1:
                return self.reliable_models[current_index + 1]
        except ValueError:
            pass
        return None

    def process_with_deepinfra(self, query: str, model: str) -> str:
        return self._process_with_openai_like_client(
            self.clients["deepinfra"], query, model
        )

    def process_with_anthropic(self, query: str, model: str) -> str:
        try:
            message = self.clients["anthropic"].messages.create(
                model=model,
                max_tokens=self.config["general"]["max_tokens"],
                temperature=self.config["general"]["temperature"],
                messages=[{"role": "user", "content": query}],
            )
            # Asegúrate de que estamos devolviendo un string
            if isinstance(message.content, list) and len(message.content) > 0:
                return message.content[0].text
            elif hasattr(message.content, "text"):
                return message.content.text
            else:
                return str(message.content)
        except Exception as e:
            logging.error(f"Error processing with Anthropic: {str(e)}")
            return f"Error al procesar con Anthropic API: {str(e)}"

    def process_with_deepseek(self, query: str, model: str) -> str:
        # DeepSeek v4 (flash/pro) y deepseek-reasoner usan "thinking mode": el
        # razonamiento consume tokens y puede dejar content vacío con max_tokens
        # bajo. Se da margen y se cae a reasoning_content si content viene vacío.
        try:
            client = self.clients.get("deepseek")
            if not client:
                return {"error": "Cliente de DeepSeek no inicializado"}
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": query}],
                temperature=self.config["general"]["temperature"],
                max_tokens=max(self.config["general"].get("max_tokens", 1000), 4000),
            )
            msg = resp.choices[0].message
            content = (msg.content or "").strip()
            if not content:
                content = (getattr(msg, "reasoning_content", None) or "").strip()
            return content or "Error: respuesta vacía de DeepSeek"
        except Exception as e:  # noqa: BLE001
            logging.error(f"Error al procesar con DeepSeek: {str(e)}")
            return f"Error al procesar con DeepSeek: {str(e)}"

    def process_with_mistral(self, query: str, model: str) -> str:
        chat_response = self.clients["mistral"].chat.complete(
            model=model, messages=[{"role": "user", "content": query}]
        )
        return chat_response.choices[0].message.content

    def process_with_cohere(self, query: str, model: str) -> str:
        response = self.clients["cohere"].chat(
            model=model,
            message=query,
            temperature=self.config["general"]["temperature"],
        )
        return response.text

    def process_with_z_ai(self, query: str, model: str) -> str:
        """Procesar consulta con Z.AI (GLM-4.6)"""
        return self._process_with_openai_like_client(
            self.clients["z-ai"], query, model
        )

    def process_with_routellm(self, query: str, model: str) -> str:
        """Procesar consulta con RouteLLM"""
        return self._process_with_openai_like_client(
            self.clients["routellm"], query, model
        )

    def process_with_local_model(self, query: str, model: str) -> str:
        try:
            url = f"{self.config['ollama']['base_url'].rstrip('/')}/api/generate"
            payload = {"model": model, "prompt": query, "stream": False}
            # Último recurso en CPU: gemma3 tarda decenas de segundos con carga.
            response = requests.post(url, json=payload, timeout=120)
            response.raise_for_status()
            return response.json()["response"]
        except Exception as e:
            log_error(f"Error al procesar con modelo local: {str(e)}")
            return f"Error al procesar con modelo local: {str(e)}"

    def process_with_api(self, query: str, model: Optional[str] = None) -> str:
        model = model or self.config["openai"]["default_model"]
        return self._process_with_openai_like_client(
            self.clients["openai"], query, model
        )

    def _load_assistant_area_map(self) -> Dict[str, str]:
        """Carga tools/assistants_map.yaml → {assistant_id: area}."""
        import os as _os
        path = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), "tools", "assistants_map.yaml"
        )
        try:
            import yaml as _yaml
            data = _yaml.safe_load(open(path))
            return {
                aid: (info or {}).get("area")
                for aid, info in (data.get("asistentes", {}) or {}).items()
                if (info or {}).get("area")
            }
        except Exception as e:  # noqa: BLE001
            logging.warning(f"No se pudo cargar assistants_map.yaml: {e}")
            return {}

    def process_with_cedula360_legal(self, area: str, query: str) -> str:
        """Procesa una consulta de un experto vía la API legal de cedula360.

        Sustituye a los OpenAI Assistants: el experto se sirve desde
        claude-for-legal-colombia (Mentor RAG por área) a través de
        POST {CEDULA360_API_BASE}/legal/ask. Requiere plan Business+.
        """
        try:
            api_key = get_secret("CEDULA360_API_KEY")
            if not api_key:
                return {"error": "CEDULA360_API_KEY no configurada"}
            base = (get_secret("CEDULA360_API_BASE") or "https://cedula360.tech/api").rstrip("/")
            r = requests.post(
                f"{base}/legal/ask",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"pregunta": query, "area": area},
                timeout=150,
            )
            if r.status_code == 403:
                return "Error: /api/legal requiere plan Business+ en cedula360"
            if r.status_code != 200:
                return f"Error al procesar con cedula360 legal: HTTP {r.status_code}"
            data = r.json()
            resp = (data.get("respuesta") or "").strip()
            return resp or (data.get("error") or "Error: respuesta vacía del Mentor legal")
        except Exception as e:  # noqa: BLE001
            logging.error(f"Error al procesar con cedula360 legal (area={area}): {str(e)}")
            return f"Error al procesar con cedula360 legal: {str(e)}"

    def process_with_assistant(self, assistant_id: str, query: str) -> str:
        # Los expertos ya no usan OpenAI: se redirigen al Mentor jurídico
        # colombiano vía cedula360 /api/legal/ask, según su área mapeada.
        area = self.assistant_area_map.get(assistant_id)
        if area:
            return self.process_with_cedula360_legal(area, query)
        # Sin área mapeada: si OpenAI está disponible, usar el Assistant clásico;
        # si no, degradar a un modelo activo con el contexto disponible.
        if not self.clients.get("openai"):
            logging.warning(
                f"Asistente {assistant_id} sin área ni OpenAI; usando área 'general'"
            )
            return self.process_with_cedula360_legal("general", query)
        try:
            thread = self.clients["openai"].beta.threads.create()
            self.clients["openai"].beta.threads.messages.create(
                thread_id=thread.id, role="user", content=query
            )
            run = self.clients["openai"].beta.threads.runs.create(
                thread_id=thread.id, assistant_id=assistant_id
            )
            while run.status != "completed":
                time.sleep(1)
                run = self.clients["openai"].beta.threads.runs.retrieve(
                    thread_id=thread.id, run_id=run.id
                )
            messages = self.clients["openai"].beta.threads.messages.list(
                thread_id=thread.id
            )
            return messages.data[0].content[0].text.value
        except Exception as e:
            logging.error(f"Error al procesar con el asistente especializado: {str(e)}")
            return None

    def process_with_together(self, query: str, model: str) -> str:
        try:
            response = self.clients["together"].chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": query}],
                temperature=self.config["together"]["temperature"],
                max_tokens=self.config["together"]["max_tokens"],
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error al procesar con Together API: {str(e)}"

    def process_with_groq(self, query: str, model: str) -> str:
        try:
            max_tokens = min(self.config["groq"]["max_tokens"], 8000)
            response = self.clients["groq"].chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": query}],
                temperature=self.config["groq"]["temperature"],
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error al procesar con Groq API: {str(e)}"

    def process_with_openrouter(self, query: str, model: str) -> str:
        try:
            # Verificar primero si el cliente existe
            if not self.clients.get("openrouter"):
                logging.warning("Cliente de OpenRouter no inicializado")
                return {"error": "Cliente de OpenRouter no inicializado"}

            # Asegurarse de que el modelo tenga el formato correcto (con prefijo del proveedor)
            if "/" not in model:
                # Buscar el modelo en la configuración para obtener el nombre completo
                found = False
                if (
                    "openrouter" in self.config
                    and "models" in self.config["openrouter"]
                ):
                    for model_config in self.config["openrouter"]["models"]:
                        if (
                            isinstance(model_config, dict)
                            and "name" in model_config
                            and model_config["name"] == model
                        ):
                            model = model_config["name"]
                            found = True
                            break

                # Si no se encuentra, usar un modelo por defecto seguro
                if not found:
                    model = "google/gemma-4-31b-it:free"
                    logging.warning(
                        f"Modelo no encontrado en la configuración, usando modelo por defecto: {model}"
                    )

            # Usar el cliente de OpenAI con la API de OpenRouter
            # Crear un diccionario con los parámetros base
            params = {
                "model": model,
                "messages": [{"role": "user", "content": query}],
                "temperature": self.config["general"]["temperature"],
                "max_tokens": self.config["general"]["max_tokens"],
            }

            # Usar el cliente de OpenAI con la API de OpenRouter
            # Crear una instancia temporal con las cabeceras correctas
            openrouter_client = OpenAI(
                api_key=get_secret("OPENROUTER_API_KEY"),
                base_url="https://openrouter.ai/api/v1",
                default_headers={
                    "HTTP-Referer": get_secret(
                        "YOUR_SITE_URL", "http://localhost:8501"
                    ),
                    "X-Title": "MALLO",
                },
            )

            # Llamar a la API con el cliente temporal
            response = openrouter_client.chat.completions.create(**params)

            if not response.choices:
                logging.warning(f"OpenRouter response has no choices: {response}")
                return "Error: Respuesta sin opciones de OpenRouter"

            message = response.choices[0].message
            content = message.content

            # Modelos de razonamiento (DeepSeek R1, o1, etc.) pueden retornar contenido vacío
            # pero con el razonamiento en un campo separado
            if not content or content.strip() == "":
                # Intentar extraer del campo reasoning si existe
                reasoning = getattr(message, 'reasoning', None)
                if reasoning:
                    logging.info(f"OpenRouter: Usando campo reasoning como contenido")
                    content = reasoning
                else:
                    logging.warning(f"OpenRouter response empty: {response}")
                    return "Error: Respuesta vacía de OpenRouter"

            return content

        except Exception as e:
            logging.error(f"Error al procesar con OpenRouter API: {str(e)}")
            return f"Error al procesar con OpenRouter API: {str(e)}"

    def process_with_ollama_cloud(self, query: str, model: str) -> str:
        try:
            if not self.clients.get("ollama_cloud"):
                logging.warning("Cliente de Ollama Cloud no inicializado")
                return {"error": "Cliente de Ollama Cloud no inicializado"}

            cloud_config = self.config.get("ollama_cloud", {})
            # Validar que el modelo exista en la configuración; si no, usar el default
            valid_models = [
                m["name"]
                for m in cloud_config.get("models", [])
                if isinstance(m, dict) and "name" in m
            ]
            if model not in valid_models:
                fallback = cloud_config.get("default_model", "gpt-oss:120b")
                logging.warning(
                    f"Modelo '{model}' no está en ollama_cloud, usando default: {fallback}"
                )
                model = fallback

            params = {
                "model": model,
                "messages": [{"role": "user", "content": query}],
                "temperature": cloud_config.get(
                    "temperature", self.config["general"]["temperature"]
                ),
                "max_tokens": cloud_config.get(
                    "max_tokens", self.config["general"]["max_tokens"]
                ),
            }

            response = self.clients["ollama_cloud"].chat.completions.create(**params)

            if not response.choices:
                logging.warning(f"Ollama Cloud response sin choices: {response}")
                return "Error: Respuesta sin opciones de Ollama Cloud"

            message = response.choices[0].message
            content = message.content

            # Modelos con "thinking mode" (gpt-oss, glm, deepseek) pueden devolver
            # content vacío y el razonamiento en un campo separado.
            if not content or content.strip() == "":
                reasoning = getattr(message, "reasoning", None) or getattr(
                    message, "thinking", None
                )
                if reasoning:
                    logging.info("Ollama Cloud: usando campo reasoning/thinking como contenido")
                    content = reasoning
                else:
                    logging.warning(f"Ollama Cloud respuesta vacía: {response}")
                    return "Error: Respuesta vacía de Ollama Cloud"

            return content

        except Exception as e:
            logging.error(f"Error al procesar con Ollama Cloud API: {str(e)}")
            return f"Error al procesar con Ollama Cloud API: {str(e)}"

    def _process_with_openai_like_client(self, client, query: str, model: str) -> str:
        try:
            # Parámetros base
            params = {
                "model": model,
                "messages": [{"role": "user", "content": query}],
            }

            # GPT-5, o1 y o3 tienen restricciones especiales:
            # - Usan max_completion_tokens en lugar de max_tokens
            # - No soportan temperature diferente de 1
            is_restricted_model = (
                model.startswith("gpt-5") or
                model.startswith("o1") or
                model.startswith("o3")
            )

            if is_restricted_model:
                params["max_completion_tokens"] = self.config["general"]["max_tokens"]
                # No agregar temperature (usa default=1)
            else:
                params["max_tokens"] = self.config["general"]["max_tokens"]
                params["temperature"] = self.config["general"]["temperature"]

            response = client.chat.completions.create(**params)
            return response.choices[0].message.content
        except Exception as e:
            return f"Error al procesar con API: {str(e)}"

    def run_moa(self, query: str, web_context: str = "") -> str:
        results = asyncio.run(self.run_multiple_models(query))
        aggregated_results = "\n".join(
            [f"{i+1}. {result}" for i, result in enumerate(results)]
        )

        full_context = (
            f"Web Context:\n{web_context}\n\nModel Responses:\n{aggregated_results}"
        )

        # Agregador sobre Ollama Cloud (config moa.api / moa.aggregator_model).
        moa_api = self.config["moa"].get("api", "ollama_cloud")
        aggregator_model = self.config["moa"]["aggregator_model"]
        aggregator_prompt = (
            f"{self.config['moa']['aggregator_system_prompt']}\n\n"
            f"Query: {query}\n\nContext:\n{full_context}"
        )
        process = getattr(self, f"process_with_{moa_api}", None)
        if process:
            return process(aggregator_prompt, aggregator_model)
        return self.process_with_ollama_cloud(aggregator_prompt, aggregator_model)

    def meta_analysis(
        self,
        query: str,
        responses: List[str],
        initial_evaluation: str,
        final_evaluation: str,
    ) -> str:
        prompt = f"""
        Analiza las siguientes respuestas a la pregunta: "{query}"

        Respuestas de los agentes:
        {json.dumps(responses, indent=2)}

        Evaluación inicial:
        {initial_evaluation}

        Tu tarea es:
        1. Evaluar la corrección de cada respuesta.
        2. Identificar la respuesta más precisa y correcta.
        3. Sintetizar una respuesta final que incorpore la información correcta.
        4. Asegurarte de que la respuesta aborde todos los puntos clave mencionados en la evaluación inicial.
        5. Proporcionar una explicación clara y concisa del razonamiento detrás de la respuesta.
        6. Mantener la respuesta enfocada y relevante a la pregunta original.

        Por favor, proporciona la respuesta sintetizada en un formato claro y estructurado.
        """

        meta_config = self.config["evaluation_models"]["meta_analysis"]

        for attempt in range(3):  # Intentar hasta 3 veces
            try:
                if attempt == 0:
                    response = self.process_query(
                        prompt, meta_config["api"], meta_config["model"]
                    )
                elif attempt == 1:
                    response = self.process_query(
                        prompt, meta_config["backup_api"], meta_config["backup_model"]
                    )
                else:
                    response = self.process_query(
                        prompt, meta_config["backup_api2"], meta_config["backup_model2"]
                    )

                if response and not response.startswith("Error"):
                    return response
            except Exception as e:
                logging.error(
                    f"Error en meta-análisis con {'modelo principal' if attempt == 0 else 'modelo de respaldo'}: {str(e)}"
                )

        return "No se pudo realizar el meta-análisis debido a múltiples errores en los modelos configurados."

    async def run_multiple_models(self, query: str) -> List[str]:
        # Propuestas del MOA sobre Ollama Cloud (moa.proposal_models del benchmark).
        moa_api = self.config["moa"].get("api", "ollama_cloud")
        proposal_models = self.config["moa"].get("proposal_models", [])
        process = getattr(self, f"process_with_{moa_api}", self.process_with_ollama_cloud)

        # La cuenta de Ollama Cloud limita las peticiones simultáneas («too many
        # concurrent requests»): las propuestas van de una en una salvo que
        # moa.max_concurrency diga otra cosa.
        sem = asyncio.Semaphore(int(self.config["moa"].get("max_concurrency", 1) or 1))

        async def run_model(model):
            async with sem:
                return await asyncio.to_thread(process, query, model)

        tasks = [run_model(model) for model in proposal_models]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, str) and r and not r.startswith("Error")]

    def internet_available(self, host="8.8.8.8", port=53, timeout=3):
        try:
            socket.setdefaulttimeout(timeout)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            return True
        except socket.error:
            return False

    def get_available_agents(self, agent_type: str = None) -> List[str]:
        if agent_type:
            models = {
                "api": self.openai_models,
                "groq": self.config["groq"]["models"],
                "together": self.together_models,
                "local": self.ollama_models,
                "openrouter": self.config["openrouter"]["models"],
                "deepinfra": self.config["deepinfra"]["models"],
                "anthropic": self.config["anthropic"]["models"],
                "deepseek": self.config["deepseek"]["models"],
                "mistral": self.config["mistral"]["models"],
                "cohere": self.config["cohere"]["models"],
                "z-ai": self.config["z-ai"]["models"],
                "routellm": self.config["routellm"]["models"],
            }.get(agent_type, [])

            # Procesar los modelos para manejar diferentes formatos
            processed_models = []
            for model in models:
                if isinstance(model, dict) and "name" in model:
                    processed_models.append(model["name"])
                elif isinstance(model, str):
                    processed_models.append(model)
            return processed_models
        else:
            all_agents = []
            for agent_type in [
                "api",
                "groq",
                "together",
                "local",
                "openrouter",
                "deepinfra",
                "anthropic",
                "deepseek",
                "mistral",
                "cohere",
                "z-ai",
                "routellm",
            ]:
                all_agents.extend(self.get_available_agents(agent_type))
            return all_agents

    def get_agent_priority(self):
        priority = self.config.get("processing_priority", [])
        agents = []
        for agent_type in priority:
            if agent_type == "specialized_assistants":
                agents.extend(
                    [
                        (agent_type, assistant["id"])
                        for assistant in self.specialized_assistants
                    ]
                )
            elif agent_type in [
                "api",
                "groq",
                "together",
                "local",
                "openrouter",
                "deepinfra",
                "anthropic",
                "deepseek",
                "mistral",
                "cohere",
                "z-ai",
                "routellm",
            ]:
                # Verificar si el cliente está inicializado
                if agent_type not in ["local"] and not self.clients.get(agent_type):
                    logging.warning(
                        f"Cliente {agent_type} no inicializado, omitiendo en prioridad"
                    )
                    continue

                # Obtener modelos disponibles
                models = self.get_available_agents(agent_type)
                if not models:
                    logging.warning(f"No hay modelos disponibles para {agent_type}")
                    continue

                agents.extend([(agent_type, model) for model in models])
        return agents


def evaluate_query_complexity(query: str) -> float:
    try:
        clean_query = re.sub(r"[^\w\s]", "", query.lower())
        words = clean_query.split()
        unique_words = set(words)
        word_complexity = min(len(unique_words) / 10, 1.0)
        length_complexity = min(len(words) / 20, 1.0)
        complexity = (word_complexity + length_complexity) / 2
        return complexity
    except Exception as e:
        logging.error(f"Error al evaluar la complejidad de la consulta: {str(e)}")
        return 0.5
