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
        self.cohere_client = cohere.Client(api_key=get_secret("COHERE_API_KEY"))

    # Seleccionar el agente más adecuado para una consulta
    def select_agent(self, query: str) -> Tuple[str, float]:
        query_embedding = self.get_embedding(query)
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

    # Obtener el agente de reserva
    def get_embedding(self, text: str) -> List[float]:
        response = self.cohere_client.embed(
            texts=[text], model="embed-english-v3.0", input_type="search_query"
        )
        return response.embeddings[0]

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
    def __init__(self, config: Dict[str, Any]):
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
        self.available_models = self.verify_models()
        self.reliable_models = []
        self.default_local_model = config["ollama"].get(
            "default_model", "phi3.5:latest"
        )
        self.processing_priority = config.get("processing_priority", [])
        self.default_agent = ("openrouter", config["deepinfra"]["default_model"])
        self.backup_default_agent = ("deepinfra", config["openrouter"]["default_model"])
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

        # Inicializar los clientes de API
        self.clients = {}

        # Inicializar cliente de OpenAI (API)
        openai_client = self.init_openai_client()
        if openai_client:
            self.clients["openai"] = openai_client
            self.clients["api"] = openai_client  # Alias para compatibilidad

        # Inicializar otros clientes
        together_client = self.init_together_client()
        if together_client:
            self.clients["together"] = together_client

        groq_client = self.init_groq_client()
        if groq_client:
            self.clients["groq"] = groq_client

        deepinfra_client = self.init_deepinfra_client()
        if deepinfra_client:
            self.clients["deepinfra"] = deepinfra_client

        anthropic_client = self.init_anthropic_client()
        if anthropic_client:
            self.clients["anthropic"] = anthropic_client

        deepseek_client = self.init_deepseek_client()
        if deepseek_client:
            self.clients["deepseek"] = deepseek_client

        mistral_client = self.init_mistral_client()
        if mistral_client:
            self.clients["mistral"] = mistral_client

        cohere_client = self.init_cohere_client()
        if cohere_client:
            self.clients["cohere"] = cohere_client

        openrouter_client = self.init_openrouter_client()
        if openrouter_client:
            self.clients["openrouter"] = openrouter_client

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

        response = self.clients["cohere"].generate(
            model=self.config["agent_selection"]["validation_model"],
            prompt=validation_prompt,
            max_tokens=300,
            temperature=0.7,
        )

        recommended_agent, confidence = self.extract_recommendation(
            response.generations[0].text
        )

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
                "type": "specialized",
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

        response = self.process_query(
            validation_prompt,
            config["default_model"].split(":")[0],
            config["default_model"].split(":")[1],
        )

        # Extraer el tipo recomendado y la confianza de la respuesta
        recommended_type, confidence = self.extract_recommendation(response)

        # Si la confianza es alta, usar el tipo recomendado; de lo contrario, mantener el inicial
        if confidence == "alta":
            return recommended_type
        else:
            return initial_type

    # Extraer la recomendación y la confianza de la respuesta generada
    def extract_recommendation(self, response: str) -> Tuple[str, str]:
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

        # Primero, intentar seleccionar un modelo especializado para el prompt_type
        specialized_models = find_specialized_models(prompt_type)
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
                prioritized_agents.append(
                    ("specialized", specialized_agent["id"], specialized_agent["name"])
                )
                used_agent_types.add("specialized")

        # Añadir agentes generales hasta tener un total de 3 agentes
        general_agents = self.get_general_agents(query, complexity, prompt_type)
        for agent in general_agents:
            if len(prioritized_agents) >= 3:
                break
            agent_type, model, name = agent
            if agent_type not in used_agent_types and model not in used_models:
                prioritized_agents.append(agent)
                used_agent_types.add(agent_type)
                used_models.add(model)

        # Si aún no tenemos 3 agentes, añadir agentes por defecto
        default_agents = [
            ("groq", self.config["groq"]["default_model"], "Groq Default"),
            (
                "openrouter",
                self.config["openrouter"]["default_model"],
                "OpenRouter Default",
            ),
            (
                "deepinfra",
                self.config["deepinfra"]["default_model"],
                "DeepInfra Default",
            ),
        ]
        for agent_type, model, name in default_agents:
            if len(prioritized_agents) >= 3:
                break
            if agent_type not in used_agent_types and model not in used_models:
                prioritized_agents.append((agent_type, model, name))
                used_agent_types.add(agent_type)
                used_models.add(model)

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
                # Probar el cliente con un modelo gratuito
                client.chat.completions.create(
                    model="openai/gpt-3.5-turbo",
                    messages=[{"role": "user", "content": "Test"}],
                    max_tokens=5,  # Usar pocos tokens para la prueba
                )
                return client
            except Exception as e:
                logging.error(f"Error checking OpenRouter API: {str(e)}")
        return None

    # Procesar la consulta con el agente seleccionado
    def get_ollama_models(self) -> List[str]:
        try:
            response = requests.get(
                f"{self.config['ollama']['base_url']}/api/tags", timeout=5
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
    ) -> str:
        start_time = time.time()
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
                fallback_agent, fallback_model = self.get_fallback_agent()
                if fallback_agent != agent_type or fallback_model != agent_id:
                    logging.info(
                        f"Attempting fallback with {fallback_agent}:{fallback_model}"
                    )
                    return self.process_query(
                        query,
                        fallback_agent,
                        fallback_model,
                        prompt_type,
                        fallback_attempts + 1,
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

    def get_fallback_agent(self) -> Tuple[str, str]:
        if self.clients.get("openrouter"):
            return "openrouter", self.config["openrouter"]["default_model"]
        elif self.clients.get("groq"):
            return "groq", self.config["groq"]["default_model"]
        elif self.clients.get("deepseek"):
            return "deepseek", self.config["deepseek"]["default_model"]
        elif self.clients.get("openai"):
            return "api", self.config["openai"]["default_model"]
        elif self.clients.get("together"):
            return "together", self.config["together"]["default_model"]
        else:
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
        return self._process_with_openai_like_client(
            self.clients["deepseek"], query, model
        )

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

    def process_with_local_model(self, query: str, model: str) -> str:
        try:
            url = f"{self.config['ollama']['base_url']}/api/generate"
            payload = {"model": model, "prompt": query, "stream": False}
            response = requests.post(url, json=payload, timeout=30)
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

    def process_with_assistant(self, assistant_id: str, query: str) -> str:
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
                    model = "deepseek/deepseek-r1-0528:free"
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
            if not response.choices or not response.choices[0].message.content:
                 logging.warning(f"OpenRouter response empty or invalid: {response}")
                 return "Error: Respuesta vacía de OpenRouter"
            
            return response.choices[0].message.content

        except Exception as e:
            logging.error(f"Error al procesar con OpenRouter API: {str(e)}")
            return f"Error al procesar con OpenRouter API: {str(e)}"

    def _process_with_openai_like_client(self, client, query: str, model: str) -> str:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": query}],
                temperature=self.config["general"]["temperature"],
                max_tokens=self.config["general"]["max_tokens"],
            )
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

        aggregator_response = self.clients["together"].chat.completions.create(
            model=self.config["moa"]["aggregator_model"],
            messages=[
                {
                    "role": "system",
                    "content": self.config["moa"]["aggregator_system_prompt"],
                },
                {
                    "role": "user",
                    "content": f"Query: {query}\n\nContext:\n{full_context}",
                },
            ],
            temperature=self.config["general"]["temperature"],
            max_tokens=self.config["general"]["max_tokens"],
        )
        return aggregator_response.choices[0].message.content

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
        async def run_model(model):
            return self.process_with_together(query, model)

        tasks = [run_model(model) for model in self.together_models]
        results = await asyncio.gather(*tasks)
        return results

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
                "openrouter": self.config["openrouter"]["models"],  # Añadido OpenRouter
                "deepinfra": self.config["deepinfra"]["models"],
                "anthropic": self.config["anthropic"]["models"],
                "deepseek": self.config["deepseek"]["models"],
                "mistral": self.config["mistral"]["models"],
                "cohere": self.config["cohere"]["models"],
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
