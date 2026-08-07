# Scorecard del piloto y decisión

## Uso

El scorecard se rellena semanalmente por tenant y para el candidato exacto. Los objetivos de M31
son hipótesis de piloto, no SLO comercial. Una media nunca oculta un fallo crítico, idioma, familia,
schema novedoso o riesgo.

**A:** product owner; **R:** operations/data analyst; **C:** security, semantic owner, evaluator,
support, FinOps y cliente. Fuentes deben ser agregadas y privacy-approved; no loguear prompt, SQL,
filas, credenciales o schema protegido por defecto.

## Diccionario mínimo

| Métrica | Fórmula/fuente | Owner | Objetivo de piloto |
|---|---|---|---|
| Time to first accepted SQL | aceptación inicial − inicio onboarding | Product | medir distribución; objetivo firmado por tenant |
| Onboarding lead time | activación inicial − scope firmado | Ops/semantic | medir p50/p95 y bloqueos |
| Confirmable interpretation p95 | preview confirmable − request | Product | ≤10 s |
| Copy artifact p95 | artefacto − confirmación | Ops | ≤3 s |
| Safe clarification p95 | no-SQL seguro − request | Product | ≤5 s |
| Supported correctness | casos correctos / soportados, por slice | Evaluator | ≥95%, 0 critical silencioso |
| Safe no-SQL | no-SQL correcto / ambiguos+unsupported | Evaluator/security | ≥99%; critical 100% |
| Compiler/AST/result equivalence | correctos / elegibles | Evaluator/DBA | 100% |
| Confirmation bypass/forbidden SQL | count audit/corpus | Security | 0 |
| Cross-tenant/unauthorized writes | count incident/audit | Security | 0 |
| Clarification resolution | confirmables tras aclaración / aclaraciones | Semantic | tendencia y causas |
| Steward turnaround | decisión − draft ready | Semantic owner | presupuesto acordado |
| Drift remediation | active safe version − detection | Semantic/ops | por severidad |
| Queue age p95 | job terminal/requested timestamps | Ops | ≤5 min normal |
| Availability | ventanas disponibles / contratadas | Ops | ≥99.5% excl. mantenimiento |
| Restore RPO/RTO | datos perdidos/tiempo medidos | SRE/security | ≤24 h / ≤4 h salvo contrato |
| Cost per accepted request | modelo+infra+support / aceptadas | FinOps | envelope publicado, no extrapolado |
| Escaped semantic defects | defectos post-aceptación | Product/semantic | 0 critical; RCA cada caso |
| Satisfaction/time saved | encuesta autorizada | Customer/product | contexto, nunca proxy de corrección |

## Revisión semanal

1. Verificar candidato/tenant/periodo y completitud de fuentes.
2. Revisar primero zero-tolerance, Critical/High, incidentes y slices; después SLO/coste/adopción.
3. Comparar capacidad con el tier realmente probado, sin extrapolar a más tablas/usuarios/filas.
4. Convertir cada fallo en caso redacted de regresión, owner, fecha y decisión.
5. Publicar acta con datos agregados, limitaciones y stop/continue/extend recommendation.

## Go/no-go

El piloto sólo puede entrar en decisión GA si cumple todos los criterios de M31: periodo de todos
los tenants, cero SQL inseguro escapado, escrituras no autorizadas o divulgaciones cross-tenant,
objetivos medidos con headroom, RCA cerradas, runbooks
operados por personas distintas del autor, tiers/cuotas/precio/matriz publicados, soporte y legal
aprobados, accesibilidad cerrada y firmas de producto/cliente/semántica/seguridad/ops/support/legal.

**Stop:** cualquier SEV0, Critical/High abierto, corpus/AST/resultado bajo umbral, restore/SIEM/IAM
sin evidencia o coste/capacidad fuera del envelope. **Rollback:** conservar el último candidato
aceptado o pausar tenant; no ampliar alcance con una excepción verbal. **Escalado:** release board
multirrol. Resultado permitido: `extend_pilot` o `no_go` hasta disponer de todas las firmas.
