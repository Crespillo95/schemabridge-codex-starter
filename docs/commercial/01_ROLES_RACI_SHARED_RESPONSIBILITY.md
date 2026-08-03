# Roles, RACI y responsabilidad compartida

## Alcance

Este contrato asigna personas distintas a significado, publicación, operación y auditoría. Los
identificadores deben ser grupos/roles gestionados, no cuentas compartidas. El tenant designa
suplente para cada owner y conserva un contacto de incidente 24×7 durante campaña/piloto.

## Roles

| Rol | Responsabilidad principal | No puede hacer por sí solo |
|---|---|---|
| Business owner | alcance, criticidad, aceptación del caso de uso | aprobar semántica técnica o release |
| Product owner | contrato/SKU, criterios de aceptación y riesgo de producto | certificar semántica, seguridad u operación en solitario |
| Semantic owner | autoridad sobre modelo, mappings, joins y políticas semánticas | publicar, activar o sustituir decisiones steward |
| Operations owner | capacidad, SLO, recuperación y aceptación del entorno operado | autoaprobar release o cambiar semántica |
| Analyst | redactar necesidad y borradores M33; usar M32 | aprobar/publicar/activar |
| Steward | definición, mapping, join, `NULL`, fanout y decisiones | publicar con credencial DataHub |
| Publisher | confirmar candidato exacto y operar M34 | crear su propia decisión semántica o activar |
| Platform operator | despliegue, migración, M23, backup/restore | alterar una versión de registro |
| Security owner | IdP/IAM/secretos/red/SIEM/pentest | aceptar riesgo de producto en solitario |
| Legal/privacy owner | DPA, retención/borrado, residencia y notificación legal | aprobar riesgo técnico o release en solitario |
| Auditor | lectura de evidencia y trazabilidad | mutar workflow, registro o fuente |
| Support/on-call | triage, comunicación y escalado | editar SQL/semántica detrás del usuario |
| Independent evaluator | corpus/answer key, equivalencia y adjudicación | modificar candidato o fixtures de desarrollo |
| Release owner | freeze, manifest, artefactos y go/no-go | autoaprobar su propia promoción |

Los nombres funcionales usados en los procedimientos son delegaciones registradas, no nuevas
autoridades de firma: `Product` es Product owner; `Semantic` y `M26 owner` son Semantic owner;
`Operations`/`Ops` son Operations owner; `Security` es Security owner; `Legal` es Legal/privacy
owner; `Support` es Support/on-call; SRE, platform/activation/catalog operator, FinOps y DBA ejecutan
bajo Operations owner; IdP/IAM/DataHub admin ejecutan el control que les asigne Security owner u
Operations owner; publisher worker/operator ejecuta bajo Publisher; incident commander/service
owner ejecuta bajo Support/on-call; Tenant admin ejecuta la administración delegada por Business
owner; `business`, `customer` y `tenant` designan al Business owner;
`evaluator` designa al Independent evaluator. Cada delegado conserva identidad individual y
separación de funciones; este mapeo no permite que una persona firme dos autoridades incompatibles.

## RACI

`A` es accountable, `R` ejecuta, `C` debe ser consultado e `I` informado.

| Actividad | A | R | C | I |
|---|---|---|---|---|
| Firmar alcance/datos/límites | Business owner | Product owner | Security owner, Legal/privacy owner, steward | Operations owner, Support/on-call |
| Desplegar y migrar | Operations owner | Platform operator + SRE | Security owner, DBA | Product owner, Business owner |
| OIDC, grupos y JML | Security owner | IdP admin | Tenant admin, auditor | Support/on-call |
| Secretos/IAM fuente/DataHub | Security owner | IAM/DBA/DataHub admins | Publisher, SRE | Auditor |
| Inventario y capacidad | Operations owner | Catalog operator | DBA, steward | Product owner |
| M33 modelo/mappings | Semantic owner | Analyst + steward | Business owner | Publisher |
| M34 publicación/read-back | Release owner | Publisher worker/operator | Steward, DataHub admin | Auditor |
| M23 activación/rollback | Platform operator | Activation operator | Publisher, steward | Business owner, Support/on-call |
| M35 cambio/remediación | Semantic owner | Steward + publisher | M26 owner, Business owner | Auditor, Support/on-call |
| M32 consulta/copia | Business owner | Analyst | Steward si hay ambigüedad | Auditor según política |
| Incidente/DR | Security owner (SEV0); Product owner + Security owner (SEV1); Operations owner (SEV2); Product owner (SEV3) | Incident commander + SRE/Support/Semantic según severidad | Legal/privacy owner, Business owner y owners afectados | Auditor y stakeholders según addendum |
| Offboarding | Business owner | Tenant admin + SRE | Legal/privacy owner, Security owner, auditor | Support/on-call, Product owner |
| M30 go-no-go | Product owner | Release owner | Security owner, Operations owner, Semantic owner, Independent evaluator | Business owner, Legal/privacy owner |
| M31/GA go-no-go | Product owner | Release owner | Business owner, Semantic owner, Security owner, Operations owner, Support/on-call, Legal/privacy owner | Auditor |

## Autoridades y firmas por puerta

| Puerta | Autoridad exigida | Qué demuestra | Qué no demuestra |
|---|---|---|---|
| Phase 1a, autenticación del manifiesto | signer workflow exacto y reviewer independiente del environment | bytes canónicos ligados al candidato y al workflow | ninguna firma de owner, control material aprobado, ejecución o release |
| M30, control `candidate_owner_signatures` | Product, Semantic, Security, Operations y Release owners, todos distintos | aprobación candidate-specific de las cinco autoridades del control | adjudicación independiente del corpus/pentest |
| M30, evidencia independiente | Independent evaluator/assessor | answer key, equivalencia, pentest y adjudicación que le corresponda | aprobación de producto, semántica, seguridad, operaciones o release |
| M30, decisión final | las cinco firmas de owner anteriores más la adjudicación independiente vinculada | `go` o `no_go` para un candidato exacto tras los 24 controles | entrada de un tenant o GA |
| M31, decisión GA | Product, Business/customer, Semantic, Security, Operations, Support y Legal | decisión GA candidate-specific después del piloto aceptado | otro dialecto, límites mayores o un tenant no evaluado |

La fila de incidente/DR se interpreta siempre con la severidad y los owners exactos del runbook de
incidentes; no existe un accountable genérico que pueda sustituirlos. La decisión M31/GA requiere
las siete firmas de la tabla, incluidas Semantic y Support.

Un fingerprint de clave, un reviewer de GitHub o una atestación de workflow no cuenta como firma de
owner. En Phase 1a/M30, falta, duplicidad o incompatibilidad de una autoridad exigida conserva
`campaign_executable=false` y **NO-GO**; en M31 conserva piloto/GA **NO-GO** sin reinterpretar el
resultado histórico de M30.

## Joiner, mover y leaver

**Responsable:** IdP admin; **aprobador:** owner del rol; **evidencia:** ticket, claims/grupos antes y
después, sesiones revocadas y access review.

1. Alta: asignar el mínimo grupo tenant-bound y probar sólo las operaciones autorizadas.
2. Cambio: retirar primero el grupo anterior, renovar token/sesión y ejecutar pruebas negativas.
3. Baja: revocar sesiones/tokens y grupos, rotar secretos compartidos indebidamente y revisar
   acciones desde la última recertificación.
4. Recertificación: mensual durante piloto; trimestral como máximo después, sujeto a contrato.

**Stop condition:** cuenta compartida, claim sin tenant, grupo incompatible, token demasiado largo
o imposibilidad de revocación. **Rollback:** retirar acceso y volver al último conjunto aprobado.
**Escalado:** Security owner y auditor; una sospecha de uso indebido entra en el runbook de
incidentes.

## Break-glass y suplencias

No existe bypass de aprobación en la aplicación. Un acceso de emergencia pertenece al IdP/cluster
operado, requiere dos personas, motivo, caducidad corta, alerta SIEM y revisión posterior. Nunca
inyecta credencial writer en web/API ni permite DDL/DML en fuente. Si esa capacidad externa no
está configurada y ensayada, el servicio se detiene en lugar de improvisarla.

## Responsabilidad compartida

SchemaBridge es responsable del contrato tipado, compilador/guard, aislamiento de componentes,
artefactos y runbooks del producto. El cliente es responsable de clasificar datos, autorizar
schemas, otorgar mínimo privilegio, mantener el editor/base donde pega SQL y decidir uso del
resultado. Ambos son responsables de owners, pruebas de aceptación, límites, incidentes, cambios y
retirada. Copiar SQL no transporta permisos ni certifica otra base de datos.
