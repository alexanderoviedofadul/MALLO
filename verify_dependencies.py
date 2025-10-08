#!/usr/bin/env python3
"""
Script para verificar que todas las dependencias críticas estén instaladas correctamente.
Ejecutar antes de hacer commit para asegurar que el despliegue funcionará.
"""

import sys
import importlib
from typing import List, Tuple

# Dependencias críticas que DEBEN estar instaladas
CRITICAL_DEPENDENCIES = [
    "streamlit",
    "openai",
    "groq",
    "anthropic",
    "mistralai",
    "cohere",
    "together",
    "replicate",
    "pydantic",
    "dotenv",  # python-dotenv
    "yaml",  # pyyaml
    "requests",
    "PIL",  # pillow
]

# Dependencias opcionales (advertencia si faltan)
OPTIONAL_DEPENDENCIES = [
    "spacy",
    "pytesseract",
    "polars",
    "aiohttp",
    "sklearn",  # scikit-learn
    "numpy",
    "cachetools",
    "orjson",
    "tenacity",
]

def check_dependency(module_name: str) -> Tuple[bool, str]:
    """
    Verifica si un módulo está instalado.
    
    Args:
        module_name: Nombre del módulo a verificar
        
    Returns:
        Tupla (instalado, versión)
    """
    try:
        module = importlib.import_module(module_name)
        version = getattr(module, "__version__", "unknown")
        return True, version
    except ImportError:
        return False, ""

def main():
    """Función principal de verificación."""
    print("=" * 70)
    print("VERIFICACIÓN DE DEPENDENCIAS DE MALLO")
    print("=" * 70)
    print()
    
    all_critical_ok = True
    missing_critical = []
    missing_optional = []
    
    # Verificar dependencias críticas
    print("📦 DEPENDENCIAS CRÍTICAS:")
    print("-" * 70)
    for dep in CRITICAL_DEPENDENCIES:
        installed, version = check_dependency(dep)
        if installed:
            print(f"✅ {dep:20s} - v{version}")
        else:
            print(f"❌ {dep:20s} - NO INSTALADO")
            all_critical_ok = False
            missing_critical.append(dep)
    
    print()
    
    # Verificar dependencias opcionales
    print("📦 DEPENDENCIAS OPCIONALES:")
    print("-" * 70)
    for dep in OPTIONAL_DEPENDENCIES:
        installed, version = check_dependency(dep)
        if installed:
            print(f"✅ {dep:20s} - v{version}")
        else:
            print(f"⚠️  {dep:20s} - NO INSTALADO (opcional)")
            missing_optional.append(dep)
    
    print()
    print("=" * 70)
    
    # Resumen
    if all_critical_ok:
        print("✅ TODAS LAS DEPENDENCIAS CRÍTICAS ESTÁN INSTALADAS")
        if missing_optional:
            print(f"⚠️  {len(missing_optional)} dependencias opcionales faltantes")
            print(f"   Faltantes: {', '.join(missing_optional)}")
        print()
        print("✅ El despliegue debería funcionar correctamente")
        return 0
    else:
        print("❌ FALTAN DEPENDENCIAS CRÍTICAS")
        print(f"   Faltantes: {', '.join(missing_critical)}")
        print()
        print("Para instalar las dependencias faltantes:")
        print("   pip install -r requirements.txt")
        print()
        print("❌ El despliegue FALLARÁ sin estas dependencias")
        return 1

if __name__ == "__main__":
    sys.exit(main())

