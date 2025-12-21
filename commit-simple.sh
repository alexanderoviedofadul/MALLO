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

echo "¿Deseas hacer push a GitHub? (s/n)"
read -r response
if [[ "$response" =~ ^([sS]|[yY])$ ]]; then
    echo "Haciendo push a GitHub..."
    git push origin main
    echo ""
    echo "Push completado!"
else
    echo "Operación cancelada. Puedes hacer push manualmente más tarde con: git push origin main"
fi