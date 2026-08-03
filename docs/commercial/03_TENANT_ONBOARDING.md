# Onboarding semántico de un tenant

## Resultado esperado

Un workspace aislado con catálogo completo y una versión registry-v2 aprobada, publicada con
read-back y activada por una operación separada. La fuente no se modifica y M33 no lee filas.

## Prerequisitos y responsables

- **A:** semantic owner; **R:** analyst/steward/publisher; **C:** platform, DBA, DataHub, security.
- setup/identidades del tenant aceptados; catálogo y scope exactos disponibles;
- importación batch no existe: un alta grande necesita planificación explícita y no debe simularse
  mediante requests enormes;
- la API no tiene aún OpenAPI comercial publicado; usar los contratos/versiones del candidato y
  registrar cada `Idempotency-Key` sin incluir credenciales.

## Procedimiento

1. Registrar la conexión mediante `POST /v1/catalog/connections` con una referencia opaca de
   binding, nunca DSN/token. Verificar aislamiento y pruebas negativas cross-tenant.
2. Solicitar refresh en `/v1/catalog/connections/{connection_id}/refreshes`. Esperar `completed`,
   registrar generación, counts y fingerprint; una generación `staging` no es consultable.
3. Recorrer assets/fields con keyset pagination hasta reconciliar los conteos acordados. Cualquier
   truncado, duplicado, alias físico conflictivo o capacidad excedida bloquea el alta.
4. Ejecutar `POST /v1/semantic-onboarding/preflight`; el servidor deriva workspace/scope,
   generación, locators, tipos, URN observada, fingerprints y base del registro en un snapshot.
5. Crear el draft M33 y registrar decisiones separadas de modelo/mappings. Todo empieza
   `needs_review`; un nombre parecido nunca es evidencia suficiente.
6. El steward aporta rationale/evidencia/riesgos y aprueba o rechaza. Transformaciones son pasos
   tipados cerrados; `parse_date` permanece bloqueado salvo contrato total.
7. Un publisher distinto confirma la huella exacta y prepara `ready_for_publication`; salida M33:
   JSON inmutable, `external_writes_performed=false`.
8. Crear job M34 en `POST /v1/registry-publications`. El publisher worker aislado reserva target,
   ensambla el candidato completo y queda `awaiting_approval`.
9. Confirmar candidato exacto y autorizar. La primera creación de un target ausente permite
   exactamente una escritura DataHub. Un replay exacto o la recuperación de un transporte ambiguo
   puede realizar cero escrituras nuevas y sólo aceptar éxito tras read-back/auditoría exactos; más
   de una escritura o un target diferente bloquea. Nunca borrar/sobrescribir para resolver conflicto.
10. Verificar `activation_ready`. M34 no activa. Usar el prepare/approval/commit M23 separado para
    mover el puntero mediante CAS y comprobar read-back/rollback.
11. Ejecutar M26 sobre la versión activa. Para joins/reemplazo/remediación usar M35; rechazo o stale
    crea una nueva fuente M33, nunca edita la anterior.

## Salida y evidencia

Guardar IDs/fingerprints/counts/estados/decisiones, autoridad de catálogo, receipt de publicación,
generación activa y pruebas negativas. No guardar valores fuente, SQL, tokens ni secretos.

## Parada, rollback y escalado

Parar ante catálogo incompleto/stale, binding ambiguo, mapping por nombre, join many-to-many sin
mitigación, self/cross-connection, publisher sin mínimo privilegio, target existente distinto,
read-back desigual o pointer cambiado automáticamente. Antes de activación, cancelar/dead-letter y
crear nuevo candidato. Después, rollback M23 crea una nueva generación hacia la última versión
aceptada. Escalar a steward, publisher y platform operator; incidentes de aislamiento/escritura van
al Security owner.
