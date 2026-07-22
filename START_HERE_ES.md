# Empieza aquí: protocolo de desarrollo con Codex

Este repositorio es el kit de arranque y el plan operativo completo de **SchemaBridge**. Está preparado para que Codex implemente el proyecto por hitos y para que tú actúes como operador y tester.

## Decisión de modelo

Usa **GPT-5.6 Sol** durante todo el proyecto.

- **High**: configuración diaria y la mayoría de hitos.
- **Extra High (`xhigh`)**: modelo de dominio, seguridad SQL, integración DataHub, matching semántico, contratos de join, interpretación del lenguaje natural y orquestación.
- **Ultra**: únicamente la auditoría final M16, donde el trabajo se divide de forma natural entre subagentes independientes.

No uses Ultra para implementar cada módulo. Incrementa coste, tiempo y coordinación sin aportar una ventaja proporcional en tareas acotadas.

## Tu papel

Tú haces cuatro cosas:

1. Ejecutas los comandos que requieran acceso local, Docker, credenciales o una aprobación explícita.
2. Realizas los tests manuales descritos en cada hito.
3. Copias errores completos, sin resumirlos.
4. Me devuelves el informe de traspaso del hito para que yo decida el siguiente paso.

Codex debe escribir el código, ejecutar pruebas automáticas, revisar el diff, actualizar documentación y preparar el informe de traspaso.

## Regla operativa

**Un hito = un chat nuevo de Codex = un commit.**

No pidas a Codex que “construya todo”. El repositorio contiene 20 hitos acotados. El estado compartido vive en archivos, no en la memoria de un chat largo.

## Primer arranque

### macOS / Linux

```bash
unzip schemabridge-codex-starter.zip
cd schemabridge-codex-starter
cp .env.example .env
bash scripts/bootstrap.sh

git init
git add .
git commit -m "chore: initialize SchemaBridge starter"

codex -m gpt-5.6-sol
```

### Windows PowerShell

```powershell
Expand-Archive schemabridge-codex-starter.zip
Set-Location schemabridge-codex-starter
Copy-Item .env.example .env
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1

git init
git add .
git commit -m "chore: initialize SchemaBridge starter"

codex -m gpt-5.6-sol
```

Marca la carpeta como **trusted** cuando Codex lo solicite. Así podrá leer `.codex/config.toml`.

## Primer prompt

Abre `prompts/M00_REPOSITORY_BASELINE.md`, copia todo su contenido y pégalo en el chat nuevo de Codex.

También puedes generar el prompt desde terminal:

```bash
.venv/bin/python scripts/render_prompt.py M00
```

En PowerShell:

```powershell
.venv\Scripts\python.exe scripts\render_prompt.py M00
```

## Ciclo de cada hito

1. Lee `plans/MASTER_PLAN.md` y el plan del hito.
2. Selecciona el nivel de razonamiento indicado.
3. Abre un chat nuevo de Codex.
4. Pega el prompt correspondiente de `prompts/`.
5. Acepta únicamente comandos razonables y mutaciones explícitas.
6. Cuando Codex termine, ejecuta el bloque **Manual test** del plan.
7. Comprueba `git diff`, `git status` y los resultados de `make check`.
8. Haz el commit sugerido.
9. Copia `tasks/HANDOFF_TEMPLATE.md`, complétalo con la salida de Codex y envíamelo.

## Qué enviarme después de cada hito

```text
Hito:
Commit:
Resultado make check:
Resultado del test manual:
Errores completos:
Decisiones o dudas de Codex:
Contenido actualizado de tasks/PROJECT_STATE.md:
```

No avances al siguiente hito cuando exista un fallo no explicado, una prueba omitida o una divergencia de arquitectura.

## Ruta crítica

Los hitos M00–M18 suman aproximadamente 63 horas. M19 es opcional y solo se inicia cuando el proyecto principal está terminado.

```text
M00 Baseline
M01 Datos sintéticos
M02 Dominio y normalización
M03 IR, compilador y guard SQL
M04 DataHub local
M05 Adaptador DataHub de lectura
M06 Matching semántico
M07 Modelos canónicos y write-back
M08 Relaciones y contratos de join
M09 Constructor guiado
M10 Planner y ejecución gobernada
M11 Lenguaje natural
M12 Orquestación del agente
M13 Persistencia y reutilización del contexto
M14 UI final
M15 Evaluación reproducible
M16 Auditoría integral con Ultra
M17 Despliegue para jueces
M18 Paquete Devpost
M19 Contribución open source opcional
```

## Restricciones que nunca se relajan

- Las bases fuente son de solo lectura.
- El LLM no genera SQL ejecutable directamente.
- El LLM produce objetos tipados; un compilador determinista genera SQL.
- Solo se ejecuta una sentencia `SELECT` o `WITH ... SELECT`.
- No se aceptan joins cartesianos.
- Máximo tres tablas por consulta en el MVP.
- Se aplican límite de filas y timeout.
- Las cardinalidades y el riesgo de fanout se muestran al usuario.
- Un mapping o contrato de join ambiguo requiere aprobación humana.
- Toda mutación en DataHub requiere confirmación explícita.
- No se utiliza información real de tu empresa.

## Documento rector

Antes de modificar código, Codex debe leer:

1. `AGENTS.md`
2. `docs/00_PRODUCT_VISION.md`
3. `docs/01_SCOPE.md`
4. `docs/02_ARCHITECTURE.md`
5. el plan del hito actual
6. `tasks/PROJECT_STATE.md`
7. `tasks/DECISION_LOG.md`

Empieza únicamente por **M00**.
