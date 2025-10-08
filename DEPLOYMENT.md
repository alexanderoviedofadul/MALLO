# Guía de Despliegue de MALLO en Streamlit Cloud

## Problema Resuelto

El error "installer returned a non-zero exit code" en Streamlit Cloud fue causado por:

1. **Dependencias de desarrollo innecesarias**: mypy, black, isort, flake8 no son necesarias en producción
2. **Versiones muy específicas**: Causaban conflictos de dependencias
3. **Modelos de Spacy pesados**: Los modelos descargados directamente aumentaban el tamaño del despliegue

## Cambios Realizados

### 1. `requirements.txt` (Optimizado para Producción)
- ✅ Eliminadas dependencias de desarrollo (mypy, black, isort, flake8)
- ✅ Versiones más flexibles para evitar conflictos
- ✅ Removidos modelos de Spacy pesados
- ✅ Mantenidas solo las dependencias esenciales para la aplicación

### 2. `requirements-dev.txt` (Nuevo)
- Contiene todas las dependencias de desarrollo
- Incluye herramientas de testing y linting
- Solo para uso local

### 3. `runtime.txt` (Nuevo)
- Especifica Python 3.11.9 para compatibilidad
- Evita que Streamlit Cloud use Python 3.13 (incompatible con algunas dependencias)

## Instrucciones de Despliegue

### Streamlit Cloud

1. **Commit y Push de los cambios**:
   ```bash
   git add requirements.txt requirements-dev.txt runtime.txt DEPLOYMENT.md
   git commit -m "Fix: Optimizar dependencias para Streamlit Cloud"
   git push origin main
   ```

2. **En Streamlit Cloud**:
   - Ve a https://share.streamlit.io/
   - Haz clic en "Reboot app" o "Manage app"
   - El despliegue debería completarse sin errores

3. **Verificar logs**:
   - Revisa los logs de instalación
   - Deberías ver: "Successfully installed..." sin errores

### Desarrollo Local

Para desarrollo local con todas las herramientas:

```bash
# Instalar dependencias de producción y desarrollo
pip install -r requirements-dev.txt

# O solo producción
pip install -r requirements.txt
```

### Modelos de Spacy (Opcional)

Si necesitas los modelos de Spacy en desarrollo local:

```bash
python -m spacy download en_core_web_sm
python -m spacy download es_core_news_sm
```

**Nota**: No instales estos modelos en Streamlit Cloud a menos que sean absolutamente necesarios.

## Solución de Problemas

### Si el error persiste:

1. **Limpiar caché de Streamlit Cloud**:
   - En el dashboard de Streamlit Cloud
   - Click en "⋮" (tres puntos) → "Clear cache" → "Reboot"

2. **Verificar secrets**:
   - Asegúrate de que todas las API keys estén configuradas en Streamlit Cloud
   - Settings → Secrets → Editar `secrets.toml`

3. **Revisar packages.txt**:
   - Verifica que los paquetes del sistema estén disponibles
   - Tesseract-OCR puede causar problemas en algunos casos

4. **Reducir dependencias**:
   - Si el problema persiste, comenta temporalmente dependencias no críticas
   - Prueba el despliegue
   - Añade dependencias una por una

## Dependencias Críticas vs Opcionales

### Críticas (No remover):
- streamlit
- openai
- groq
- anthropic
- pydantic
- python-dotenv

### Opcionales (Pueden comentarse si hay problemas):
- spacy (si no usas NLP avanzado)
- pytesseract (si no usas OCR)
- polars (si no usas procesamiento de datos grandes)
- prometheus-client (si no usas métricas)

## Monitoreo Post-Despliegue

Después del despliegue exitoso:

1. ✅ Verifica que la aplicación cargue correctamente
2. ✅ Prueba las funcionalidades principales
3. ✅ Revisa los logs para warnings
4. ✅ Monitorea el uso de recursos

## Contacto y Soporte

Si encuentras problemas adicionales:
- GitHub Issues: https://github.com/bladealex9848/MALLO/issues
- Streamlit Forum: https://discuss.streamlit.io/

---

**Última actualización**: 2025-10-08
**Versión**: 1.0.0

