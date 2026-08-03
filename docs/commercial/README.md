# Modelo operativo comercial objetivo — borrador NO-GO

## Estado y propósito

Este directorio convierte el contrato y la auditoría de
[`docs/19_COMMERCIAL_USAGE.md`](../19_COMMERCIAL_USAGE.md) en un modelo objetivo que otra persona
puede revisar. Todavía no es un manual ejecutable end-to-end: faltan OpenAPI/payloads integrados y
una consola comercial que una M33→M34→M23→M35→M32. No cambia el veredicto actual: **M30, piloto,
producción y disponibilidad comercial siguen NO-GO**. Los comandos locales y escenarios
sintéticos son preparación; no sustituyen IAM, SIEM, backup/restore, pentest, corpus ciego, firmas
ni operación en el destino.

La oferta candidata es exclusivamente PostgreSQL, copy-first, una conexión por petición, hasta
tres tablas y dos joins aprobados. El usuario confirma una interpretación tipada y copia SQL
standalone; SchemaBridge no lo ejecuta automáticamente. Otro dialecto, federación, subconsultas
arbitrarias o una ampliación de límites requieren producto y certificación separados. Mientras el
artefacto M32 pueda contener `target_fingerprint=None`, exponerlo a un tenant es un **hard stop**:
una comprobación manual del destino sirve para diagnóstico local, pero no cierra el gate comercial.

## Matriz inequívoca de bases y escala

| Escenario | Estado | Límite exacto |
|---|---|---|
| Varios clientes, cada uno con PostgreSQL aislado | objetivo de beta, hoy NO-GO | cada tenant requiere onboarding, binding, M30/M31 y operación propios |
| Miles de tablas inventariadas dentro de un tenant | catálogo paginado localmente disponible; capacidad comercial no certificada | inventariar no autoriza usarlas juntas ni elimina cuotas/scale tests |
| Una consulta en una conexión PostgreSQL | candidato acotado | máximo tres tablas y dos joins aprobados, tras binding y confirmación exactos |
| Join entre conexiones o PostgreSQL distintos | no soportado | sin federación ni transporte de autoridad entre conexiones |
| Más de tres tablas o más de dos joins | no soportado | exige contrato, fanout/coste, compilador/guard y campaña nuevos |
| MySQL, SQL Server, Oracle, BigQuery, Snowflake u otro dialecto | no soportado | cada motor es un producto certificado por separado; traducir sintaxis no basta |

## Recorrido y documentos

1. [Roles, RACI y responsabilidad compartida](01_ROLES_RACI_SHARED_RESPONSIBILITY.md).
2. [Preparación y despliegue por tenant](02_SETUP_DEPLOYMENT.md).
3. [Onboarding semántico de un tenant](03_TENANT_ONBOARDING.md).
4. [Uso diario y resolución de solicitudes no soportadas](04_DAILY_USE_AND_UNSUPPORTED.md).
5. [Incidentes, recuperación y comunicación](05_INCIDENT_RECOVERY.md).
6. [Offboarding y retirada](06_OFFBOARDING_DECOMMISSION.md).
7. [Scorecard del piloto y decisión](07_PILOT_SCORECARD.md).

Flujo completo previsto:

```text
alcance firmado → despliegue aislado → OIDC/IAM/secretos → catálogo completo
→ M33 autoría/revisión → M34 publicación/read-back → M23 activación separada
→ M35 cambio gobernado → M32 preparar/confirmar/copiar
→ operación/soporte → exportación/revocación/retención-borrado
```

Hoy no existe una consola comercial única para todo ese flujo. M33–M35 disponen de APIs y
superficies de aceptación separadas; M32 es una superficie Streamlit/CLI transitoria, sin API ni
historial compartido. Este borrador no presenta esas brechas como funciones terminadas.

## Regla de evidencia

Cada paso registra: responsable, prerequisitos, acción, salida esperada, evidencia, condición de
parada, rollback y escalado. Una casilla sólo se cierra con evidencia del tenant, entorno y
candidato exactos. Un fixture, captura local, merge ref de PR, informe autodeclarado o campo
`status=passed` no constituye evidencia operada.

M30 Phase 1a puede autenticar un manifiesto externo mediante GitHub Artifact Attestations. Un
manifiesto autenticado sólo prueba los bytes congelados: mantiene `campaign_executable=false`, los
24 controles materiales sin adjudicar y `release_decision=no_go`. El entorno independiente y sin
secretos `m30-manifest-attestation` debe configurarse y probarse externamente; declararlo en YAML
no demuestra reviewers ni ausencia de bypass. Verificar un bundle separado tampoco equivale por sí
solo a una operación air-gapped.

## Bloqueadores que no puede resolver el repositorio

- propietario independiente del corpus ciego 500 ES + 500 EN y answer key;
- entorno objetivo, IdP, DataHub, source, secret manager, cluster, SIEM y almacenamiento remoto;
- pentest/assessor independiente y responsables con autoridad de firma;
- acuerdos DPA, privacidad, retención/borrado, subprocesadores, residencia, soporte y facturación;
- uno a tres design partners autorizados y 30–60 días de piloto medido.

Si falta uno de esos elementos, el operador conserva NO-GO y escala al owner indicado; no activa
un modo local/fake ni reduce una validación para avanzar.
