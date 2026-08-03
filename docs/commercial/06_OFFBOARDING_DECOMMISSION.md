# Offboarding y retirada de un tenant

## Resultado esperado

Acceso y procesamiento detenidos, evidencia exportada, identidades/secretos revocados, conexiones
deshabilitadas y datos del control plane/backups tratados según retención, legal hold y DPA. Nunca
se borra ni modifica la base fuente como parte del offboarding.

Este flujo todavía no está automatizado extremo a extremo; es un bloqueador P0 del piloto. Cada
paso manual necesita ticket, doble revisión y evidencia tenant-bound.

## Prerequisitos y responsables

- **A:** customer/business owner; **R:** tenant admin + SRE; **C:** legal, security, auditor,
  semantic/DataHub owners; **I:** soporte/product.
- fecha de cierre, alcance, DPA/retención/borrado/legal hold, export recipient y claves;
- inventario de usuarios/grupos/sesiones, secretos, conexiones, jobs, registry/active pointer,
  DataHub documents, control tables, object storage y backups.

## Procedimiento

1. Congelar altas/cambios/publicaciones y avisar la ventana. Rechazar nuevas requests; drenar o
   cancelar jobs con fencing, sin perder auditoría.
2. Exportar por canal cifrado el inventario y auditoría acordados, con counts/digests/periodo y
   receptor. No exportar secretos, prompts, SQL o filas salvo base legal y flujo específico.
3. Retirar grupos OIDC, revocar sesiones/tokens y confirmar pruebas negativas. Recertificar que no
   quedan cuentas compartidas, break-glass o integraciones del tenant.
4. Revocar versiones de secret manager/connector/DataHub publisher; verificar que web/API nunca
   las tuvieron y que la versión anterior falla.
5. Deshabilitar cada conexión mediante
   `POST /v1/catalog/connections/{connection_id}/disable`. Esto es lógico y no borra fuente.
6. Detener refresh/profile/execution/publication/reconciliation del tenant y verificar colas,
   leases/dead letters. Preservar incidentes o publicaciones parciales.
7. Registrar la última active generation/registry y retirar exposición según contrato. Los
   documentos DataHub/versiones inmutables no se sobrescriben; borrado físico necesita una
   operación DataHub autorizada separada que hoy no ofrece este runbook.
8. Aplicar retención/borrado a control plane, logs, artifacts y backups mediante herramientas del
   proveedor. Object lock/legal hold prevalece; no simular borrado editando tablas.
9. Repetir búsquedas de identidad/ruta/secret/job/datos del tenant y pruebas de acceso negativo.
10. Emitir certificado de retirada firmado: scope, counts/digests, excepciones retenidas, fecha de
    eliminación futura, owners y evidencias.

## Stop, rollback y escalado

Parar ante tenant/objeto ambiguo, legal hold desconocido, export no verificada, job activo,
credencial no revocable, target compartido o ausencia de backup previo cuando el contrato lo exige.
Antes del borrado irreversible puede revertirse reabriendo la ventana con aprobación. Después de
una eliminación certificada no existe rollback: sólo puede restaurarse bajo legal hold o base
jurídica documentada, reabriendo formalmente el proceso y fijando una nueva fecha obligatoria de
eliminación. Cualquier sospecha de cross-tenant o pérdida de datos es SEV0/SEV1 y pasa al incident
commander/legal.

## Checklist de cierre

- [ ] tráfico/jobs/publicaciones detenidos y auditoría preservada;
- [ ] export cifrado verificado por receptor;
- [ ] OIDC/sesiones/tokens/grupos revocados;
- [ ] secretos y capacidades DataHub/source revocados con pruebas negativas;
- [ ] conexiones deshabilitadas, sin escritura a fuente;
- [ ] registry/DataHub/control/logs/backups clasificados por retención/borrado/hold;
- [ ] búsquedas residuales y access review cerradas;
- [ ] certificado y excepciones firmados por cliente, seguridad, operaciones y legal.
