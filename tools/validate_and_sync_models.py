
import os
import sys
import yaml
import logging
from typing import Dict, List, Any
import requests
from openai import OpenAI
from groq import Groq
from together import Together
import cohere
import time

# Configuración de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Cargar secretos
try:
    from load_secrets import load_secrets, get_secret
    load_secrets()
except ImportError:
    # Fallback si se ejecuta desde tools/
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from load_secrets import load_secrets, get_secret
    load_secrets()

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
    with open(config_path, 'r') as file:
        return yaml.safe_load(file)

def test_model(provider: str, model_name: str, config: Dict) -> bool:
    try:
        logging.info(f"Testing {provider} - {model_name}...")
        
        if provider == "openrouter":
            client = OpenAI(
                api_key=get_secret("OPENROUTER_API_KEY"),
                base_url="https://openrouter.ai/api/v1",
                default_headers={"HTTP-Referer": "http://localhost:8501", "X-Title": "MALLO Test"}
            )
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5
            )
            return True

        elif provider == "groq":
            client = Groq(api_key=get_secret("GROQ_API_KEY"))
            client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5
            )
            return True

        elif provider == "together":
            client = Together(api_key=get_secret("TOGETHER_API_KEY"))
            client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5
            )
            return True

        elif provider == "deepinfra":
            client = OpenAI(
                api_key=get_secret("DEEPINFRA_API_KEY"),
                base_url="https://api.deepinfra.com/v1/openai"
            )
            client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5
            )
            return True
            
        elif provider == "deepseek":
            client = OpenAI(
                api_key=get_secret("DEEPSEEK_API_KEY"),
                base_url="https://api.deepseek.com/v1"
            )
            client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5
            )
            return True
            
        elif provider == "cohere":
            client = cohere.Client(api_key=get_secret("COHERE_API_KEY"))
            client.chat(model=model_name, message="Hi", max_tokens=5)
            return True

        # Añadir más proveedores según sea necesario
        
    except Exception as e:
        logging.error(f"Failed {provider} - {model_name}: {str(e)}")
        return False
    
    return False

def main():
    config = load_config()
    results = {}
    
    providers_to_test = ["openrouter", "groq", "together", "deepinfra", "deepseek", "cohere"]
    
    for provider in providers_to_test:
        if provider in config:
            models_config = config[provider].get("models", [])
            print(f"\n--- Testing Provider: {provider} ---")
            for model_info in models_config:
                if isinstance(model_info, dict):
                    model_name = model_info["name"]
                else:
                    model_name = model_info
                
                success = test_model(provider, model_name, config)
                results[f"{provider}:{model_name}"] = success
                time.sleep(1) # Rate limiting safe buffer

    print("\n\n=== SUMMARY OF WORKING MODELS ===")
    working_models = [k for k, v in results.items() if v]
    for m in working_models:
        print(f"✅ {m}")
        
    print("\n=== SUMMARY OF FAILED MODELS ===")
    failed_models = [k for k, v in results.items() if not v]
    for m in failed_models:
        print(f"❌ {m}")

if __name__ == "__main__":
    main()
