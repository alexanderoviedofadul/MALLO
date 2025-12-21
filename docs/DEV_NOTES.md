# Notas de Desarrollo y Mantenimiento

Este documento registra detalles técnicos sobre correcciones, mejoras y decisiones de arquitectura que no necesariamente pertenecen al registro de cambios general o a la documentación de usuario final.

## [2025-12-21] Correcciones de Estabilidad y Configuración

### 1. Solución al Error de Argumento 'proxies' en Clientes API
**Problema:** Las librerías `groq` y `anthropic` lanzaban un error `TypeError: ... got an unexpected keyword argument 'proxies'` durante su inicialización. Esto sugería que una versión reciente de estas librerías o una configuración del entorno estaba inyectando argumentos no soportados.

**Solución:**
- Se modificó `agents.py` en el método `init_client` de la clase `AgentManager`.
- Se implementó una lógica de captura de excepción específica para `TypeError`.
- Si se detecta que el error contiene la cadena "proxies", el sistema reintenta la inicialización del cliente eliminando explícitamente el argumento `proxies` de `kwargs`.
- Se añadió logging de advertencia para rastrear cuándo ocurre esta corrección automática.

### 2. Corrección de Error de Subíndice en Búsqueda YOU
**Problema:** La función `you_search` en `utilities.py` fallaba con `TypeError: 'Results' object is not subscriptable`. Esto ocurría porque la estructura de respuesta de la librería `youdotcom` cambió o no se estaba manejando correctamente el objeto `Results`.

**Solución:**
- Se actualizó `utilities.py` para inspeccionar los tipos de datos devueltos por `you.search.unified(query=query)`.
- Se añadió una verificación robusta para asegurar que `res.results.web` sea una lista antes de intentar acceder a sus elementos mediante slicing (`[:3]`).
- Se añadieron bloques de manejo de excepciones y logging para diagnosticar mejor la estructura de la respuesta en el futuro.

### 3. Mejora en Logging de OpenRouter
**Problema:** Los fallos en OpenRouter a veces resultaban en respuestas vacías o errores genéricos difíciles de depurar.

**Solución:**
- Se mejoró `process_with_openrouter` en `agents.py`.
- Se añadieron verificaciones para asegurar que `response.choices` no esté vacío y que el contenido del mensaje sea válido antes de retornarlo.
- Se implementó un logging más descriptivo para las respuestas de error o vacías.

### 4. Cambio de Modelo de Evaluación Inicial
**Problema:** El modelo `deepseek/deepseek-r1-0528:free` a través de OpenRouter estaba devolviendo respuestas inválidas consistentemente, bloqueando el flujo de evaluación inicial.

**Solución:**
- Se modificó `config.yaml`.
- Se cambió el modelo principal de `evaluation_models.initial` a `deepseek:deepseek-chat` (usando la API directa de DeepSeek).
- Se configuró `openai:gpt-5-nano` como el modelo de respaldo principal para esta tarea.

### 5. Automatización de Commits
**Cambio:**
- Se actualizó el script `commit-simple.sh` para realizar un `git push origin main` automáticamente tras un commit exitoso.
- Se eliminó la solicitud de confirmación al usuario para agilizar el flujo de trabajo.
- Se corrigió un bug donde el script detectaba su propio código como un conflicto de merge (`<<<<` `<<< HEAD`).
