# Incidentes, recuperación y comunicación

## Estado

Este es el procedimiento objetivo del piloto. No constituye evidencia de que SIEM, paging,
retención o restore estén operados: esos controles M30 siguen abiertos y algunos artefactos de
observabilidad están explícitamente inactivos.

## Severidad y owners

| Nivel | Ejemplos | A/R y contención | Ack/actualización |
|---|---|---|---|
| SEV0 | escritura no autorizada, fuga tenant/credencial, SQL prohibido escapado | Security owner / incident commander; parar inmediatamente | ack ≤15 min; frecuencia de actualización firmada por tenant |
| SEV1 | métrica crítica silenciosamente errónea, restore fallido, autoridad perdida | Product owner + Security owner / SRE + Semantic owner; parar tenant afectado | valores tenant-bound firmados antes del piloto |
| SEV2 | cola/publicación bloqueada, degradación/SLO o drift sin escape | Operations owner / service owner; contener y comunicar | valores tenant-bound firmados antes del piloto |
| SEV3 | defecto no crítico o solicitud no soportada | Product owner / Support/on-call; backlog con workaround seguro | valores tenant-bound firmados antes del piloto |

Antes de M31, el addendum de cada tenant **debe** congelar para SEV1–SEV3 el ack, objetivo de
contención, frecuencia/canal de actualización, contactos primario/suplente y reloj de notificación
legal. El repositorio no asigna defaults ni permite heredar los de otro tenant. Un valor ausente o
un canal sin prueba de entrega mantiene el piloto en **NO-GO**. Estos tiempos son objetivos firmados
del piloto, no SLO comercial inferido.

## Procedimiento

1. **Detectar y clasificar.** Registrar timestamp UTC, tenant/candidato/componentes, reason codes y
   alerta; nunca copiar tokens, prompts, SQL, filas o traces sin sanitizar.
2. **Contener.** Retirar tráfico/feature afectada, cancelar/drain jobs cuando sea seguro, revocar
   credencial comprometida y preservar estado append-only. No borrar dead letters ni overwrite de
   registry/DataHub.
3. **Proteger fuente y tenants.** Verificar read-only, timeout, allowlist, sesiones/grupos e IDOR;
   ampliar la parada si no puede acotarse el blast radius.
4. **Comunicar.** Incident commander asigna scribe y frecuencia. Legal/security decide notificación
   según contrato; soporte comunica hechos, impacto, mitigación y próxima actualización, sin
   atribuir causa no probada.
5. **Diagnosticar.** Correlacionar candidato/digests, active generation, catálogo/dependency
   watermark, job/lease/fence, publisher receipt, secret version y eventos SIEM.
6. **Recuperar.** Para aplicación, volver al último artefacto aceptado; para semántica, M23 rollback
   crea nueva generación; para control plane, restaurar backup firmado en destino fresco siguiendo
   [`deploy/recovery/RUNBOOK.md`](../../deploy/recovery/RUNBOOK.md) y verificar antes de cutover.
7. **Validar.** Repetir pruebas positivas/negativas, corpus afectado, read-back, aislamiento y
   observabilidad. Un cambio de modelo/prompt/compiler/guard exige rerun M30 completo.
8. **Cerrar.** RCA sin culpabilización, timeline, causa/control, acciones con owner/fecha, caso
   redacted de regresión y aprobación de owners/cliente.

## Casos específicos

- **M34 parcial/dead letter:** no borrar target. Preservar reservation/lease/fence/audit, comprobar
  read-back y escalar a publisher/DataHub admin. Un target distinto es conflicto definitivo.
- **M35/drift:** bloquear dependientes; nueva propuesta/decisiones/publicación/activación. Nunca
  parchear versión activa.
- **Proveedor LLM:** deshabilitar ruta, verificar presupuesto/config fingerprint y conservar no-SQL
  seguro; no hacer fallback a proveedor/modelo no aprobado.
- **Pérdida SIEM/paging:** tratar como incidente porque la ausencia de evento no prueba salud.
- **Secret leak:** revocar antes de rotar, inventariar consumidores, reemitir versión exacta y
  demostrar que la anterior ya no funciona.

## Evidencia, stop y escalado

Bundle: incident ID, timeline, owners, alcance, eventos sanitizados, acciones, backup/restore,
RPO/RTO, validación y firmas. Parar permanentemente el tenant ante SEV0/SEV1 abierto, Critical/High
sin remediar, restore no verificable, autoridad incompleta o saturación fuera del envelope. Sólo el
go/no-go multirrol permite reanudar; presión comercial no es una excepción.
