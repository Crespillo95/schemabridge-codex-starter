# Uso diario y solicitudes no soportadas

## Promesa defendible

SchemaBridge genera PostgreSQL gobernado para un lenguaje analítico cerrado sobre contexto
previamente aprobado. No garantiza “cualquier consulta”, “cero errores” ni portabilidad a otro
gestor. La longitud —incluidas consultas de más de 50 líneas— no determina el soporte: importan la
semántica, familia tipada, autoridad y límites.

## Prerequisitos y responsables

- **A:** business owner; **R:** analyst; **C:** steward; **I:** soporte/auditor según contrato.
- mismo tenant, PostgreSQL y contexto gobernado; active pointer/catalog/dependencies actuales;
- máximo una conexión, tres tablas, dos joins, tres modelos, doce campos y cuatro ventanas;
- política de `NULL`, fanout, límite y orden definidos; preview de fuente desactivado por defecto.

## Entrada local de aceptación

Mientras no exista la API/superficie comercial integrada, el único recorrido reproducible es la
CLI local del candidato sobre contexto sintético o de evaluación autorizado:

```bash
.venv/bin/schemabridge sql-from-natural \
  --review-and-confirm \
  "Para cada mes, en pedidos completados, calcula por categoría de producto..."
```

La confirmación por defecto es `No`. Este comando es instrumentación local/acceptance: no es una
API gestionada, no habilita ejecución automática y puede mantener
`target_fingerprint=None`/`unbound-local-recorded`. No aporta evidencia M30/M31. Un operador
comercial no debe exponerlo a un tenant como superficie productiva.

## Procedimiento M32

1. Describir objetivo, grano, dimensiones, métricas, filtros/fechas, orden/empates y límite. No
   incluir SQL, credenciales ni datos sensibles en el prompt.
2. Revisar la interpretación: modelos/fields, mappings, joins, supuestos, riesgos, fanout, `NULL`,
   route v1/v2 y límites. Antes de confirmar no debe existir SQL ni descarga.
3. En managed, comprobar que el registro completo y el plan seleccionado siguen elegibles en M26,
   y antes del checkbox revisar conexión, revisión de ruta, target fingerprint y contrato de tipos
   ligados por `qsp3`. Si es exacta, confirmar la misma huella. Cualquier cambio de catálogo,
   registro, pointer, decisión semántica o target invalida la preview y obliga a empezar de nuevo.
4. Descargar/copiar el artefacto sólo cuando ambas validaciones AST lo acepten. Debe indicar
   PostgreSQL, hashes y `executed=false`, sin placeholders.
5. Pegar únicamente en un editor conectado al contexto acordado, usando identidad read-only,
   timeout/row limit y workload controls del cliente. Ejecutar allí es responsabilidad del cliente.
6. Registrar request/plan/artifact fingerprints y resultado de aceptación; no registrar prompt,
   SQL, literales o filas por defecto.

La UI actual muestra conexión, revisión y fingerprints, pero todavía no ofrece una identidad humana
no secreta y verificable de environment/base/schema/reader ni existe evidencia de fidelidad para
clipboard/descarga en pgAdmin, DBeaver y `psql`. Hasta que ambas cosas estén implementadas y
certificadas, el paso 5 es un **hard stop comercial**: un fingerprint opaco y una comprobación
manual no prueban que el editor externo apunta al destino gobernado.

El P0 está implementado localmente para staging/production y pendiente de la verificación final de
bytes exactos: el runtime exige
registry-v2, gate M26 del registro completo antes de target/proveedor y gate M26 del plan en cada
transición posterior; vuelve a resolver el target después de interpretar, al confirmar y al
generar, e incluye la misma identidad en plan/compiler/guard/renderer/guard. La UI revalida de forma
determinista y sin proveedor cualquier artefacto retenido en cada rerun antes de mostrar SQL o
descarga. Ausencia, drift semántico, revocación, rotación o sustitución producen cero SQL. El
**hard stop comercial operado** sigue vigente hasta que M30 certifique ese recorrido en el target
real. El modo local/recorded sin target es sólo diagnóstico y nunca autoriza piloto ni reutilización
entre conexiones.

## Familias

Soportadas dentro del contrato: proyección, filtros/fechas, orden/límite, agregados y condicionales,
`HAVING`, buckets, ranking/top-N, porcentajes, running/moving windows, `lag/lead`, identificadores y
semántica `NULL`. No soportadas: self/CROSS join, subconsultas/sets arbitrarios, recursión,
gaps/islands, `ROLLUP` inseguro, federación y planes sobre el límite.

## Ambigüedad y `unsupported`

| Resultado | Acción del analyst | Acción del steward/product |
|---|---|---|
| métrica/fecha/grano ambiguos | especificar definición y grano | aprobar/actualizar concepto si falta |
| join/fanout/`NULL` ambiguos | no elegir por intuición | revisar contrato y mitigación explícita |
| campo sólo físico | detener M32 | abrir M33; no promover por similitud |
| contexto stale/revocado | iniciar nueva preview | reconciliar M26/M35/activación |
| familia no soportada | reformular dentro del lenguaje o usar proceso externo | registrar demanda; no generar aproximación |
| otro dialecto/conexión federada | seleccionar producto certificado | abrir milestone/dialecto separado |

La UI actual ofrece sobre todo un código y “reescriba”; selector conversacional, enlace de ayuda y
feature-request son gaps P1. Soporte puede explicar el contrato, nunca escribir una query “parecida”
por detrás.

## Parada, rollback y escalado

Parar ante significado incompleto, mapping/join no aprobado, target incierto, SQL antes de
confirmación, placeholder, DDL/DML/utility, segunda sentencia, activo desconocido o resultado
distinto del oracle. Antes de ejecución externa, rollback es descartar el artefacto. Si ya se pegó,
cancelar en el editor/DB, preservar evidencia y evaluar impacto; SchemaBridge nunca corrige SQL
silenciosamente. Escalar semántica al steward, seguridad al incident commander y demanda de
lenguaje a product owner.
