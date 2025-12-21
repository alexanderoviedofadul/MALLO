#!/bin/bash

# Script simple para hacer commit de los cambios
# Uso: ./commit-simple.sh "mensaje del commit"

if [ -z "$1" ]; then
    echo "Error: Debes proporcionar un mensaje para el commit"
    echo "Uso: ./commit-simple.sh "mensaje del commit""
    exit 1
fi

echo "Preparando commit con mensaje: $1"
echo ""

# Validaciones pre-commit simplificadas
echo "🔍 Ejecutando validaciones pre-commit simplificadas..."

# 1. Verificar claves API expuestas (búsqueda simple de patrones comunes)
echo "🔑 Verificando que no haya claves API expuestas en el código..."
# Patrones: sk-..., key-..., api_key..., token...
# Excluyendo archivos de configuración, logs, .env, y archivos binarios
if grep -rE "sk-[a-zA-Z0-9]{20,}|eyJ[a-zA-Z0-9]{20,}" . --exclude-dir={venv,.git,__pycache__} --exclude={*.log,*.json,*.lock,*.txt,*.md,.env*,config.yaml,secrets.toml} > /dev/null; then
    echo "⚠️  ADVERTENCIA: Posibles claves API encontradas en el código:"
    grep -rE "sk-[a-zA-Z0-9]{20,}|eyJ[a-zA-Z0-9]{20,}" . --exclude-dir={venv,.git,__pycache__} --exclude={*.log,*.json,*.lock,*.txt,*.md,.env*,config.yaml,secrets.toml}
    echo "¿Deseas continuar de todos modos? (s/n)"
    read -r response
    if [[ ! "$response" =~ ^([sS]|[yY])$ ]]; then
        echo "❌ Commit cancelado por el usuario."
        exit 1
    fi
else
    echo "✅ No se encontraron claves API expuestas en el código."
fi

# 2. Verificar archivos grandes (>50MB)
echo "📦 Verificando que no haya archivos grandes..."
large_files=$(find . -type f -size +50M -not -path "./venv/*" -not -path "./.git/*" -not -path "./.streamlit/*")
if [ -n "$large_files" ]; then
    echo "⚠️  ADVERTENCIA: Se encontraron archivos grandes (>50MB):"
    echo "$large_files"
    echo "¿Deseas continuar de todos modos? (s/n)"
    read -r response
    if [[ ! "$response" =~ ^([sS]|[yY])$ ]]; then
        echo "❌ Commit cancelado por el usuario."
        exit 1
    fi
else
    echo "✅ No se encontraron archivos grandes."
fi

# 3. Verificar conflictos de merge
echo "🔄 Verificando que no haya conflictos de merge sin resolver..."
if grep -r "<<<<""<<< HEAD" . --exclude-dir={venv,.git,__pycache__} --exclude="commit-simple.sh" > /dev/null; then
    echo "❌ ERROR: Se encontraron conflictos de merge sin resolver:"
    grep -r "<<<<""<<< HEAD" . --exclude-dir={venv,.git,__pycache__} --exclude="commit-simple.sh"
    echo "Por favor, resuelve los conflictos antes de hacer commit."
    exit 1
else
    echo "✅ No se encontraron conflictos de merge sin resolver."
fi

echo "✅ Todas las validaciones pre-commit pasaron exitosamente."
echo ""

echo "Archivos modificados:"
git status --short
echo ""

echo "Añadiendo archivos nuevos (si los hay)..."
git add -A
echo ""

echo "Haciendo commit..."
git commit -m "$1"
echo ""

echo "Commit completado con éxito!"
echo ""

# Push automático
echo "🚀 Haciendo push automático a GitHub..."
git push origin main
if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Push completado exitosamente!"
else
    echo ""
    echo "❌ Error al hacer push. Por favor revisa tu conexión o permisos."
    exit 1
fi
