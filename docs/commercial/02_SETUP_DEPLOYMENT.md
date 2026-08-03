# Preparación y despliegue por tenant

## Resultado esperado

Un despliegue aislado, sin tráfico, con identidad/versiones registradas, control plane migrado,
configuración production-shaped validada y una carpeta de evidencia externa. El overlay M29 es una
referencia con placeholders: aplicarlo localmente o hacer `dry-run` no prueba el cluster objetivo.

## Prerequisitos y responsables

- **A:** Operations owner; **R:** platform operator + SRE/DBA; **C:** security, DataHub, IdP y
  tenant admins.
- alcance firmado: PostgreSQL/versiones/región/schemas/clasificación/query families/exclusiones;
- candidato aceptable, imágenes por digest y ventana de cambio;
- OIDC, secret manager, TLS, DNS, ingress/CNI, DataHub y dos PostgreSQL distintos: source read-only
  y control plane;
- rutas de backup/restore, observabilidad y contactos; nunca secretos en tickets o repositorio.

## Procedimiento

1. Crear un inventario externo con tenant/workspace opacos, región, owners, versiones, digests,
   endpoints clasificados y referencias de secretos; no copiar valores secretos.
2. Copiar el overlay a un workspace operado **fuera del checkout**, sustituir allí todos los
   placeholders con valores tenant-bound revisados y sólo entonces renderizar y validar. El
   siguiente nombre representa una ruta externa, no un archivo dentro del repositorio:

   ```bash
   kubectl kustomize /external/tenant-overlay > /external/evidence/rendered-m29.yaml
   python deploy/kubernetes/m29/validate_rendered.py /external/evidence/rendered-m29.yaml
   kubectl apply --server-side --dry-run=server -f /external/evidence/rendered-m29.yaml
   ```

3. Como prueba negativa separada, el overlay público sin configurar debe fallar el validator. No
   “arreglarlo” dentro del checkout ni conservar un render allí. Mantener imágenes digest-only,
   identidades separadas, TLS, default-deny y egress por capacidad.
4. Aplicar migraciones forward-only y verificar el esquema exacto con la identidad de migración.
   Nunca usar `control-plane-reset` en un entorno real.
5. Arrancar componentes con sus probes exactos y tráfico a cero. Web/API no reciben fuente writer
   ni DataHub writer; el publisher aislado no recibe credenciales OIDC de navegador.
6. Ejecutar pruebas positivas y negativas de identidad, red, secretos, source read-only, DataHub
   read-back y separación de tenants.
7. Conectar métricas/alertas/SIEM/paging reales y ejecutar entrega/pérdida/recuperación. Los
   artefactos bajo `deploy/observability/inactive/` siguen inactivos hasta tener productor y sink.
8. Crear backup remoto, restaurar en un destino fresco y medir RPO/RTO antes de habilitar tráfico.

## Salida y evidencia

- render final y resultado del validator/server-side admission;
- digests de imagen/configuración/migraciones y matriz de identidades/grants;
- pruebas NetworkPolicy/TLS/egress, secret version/rotation y SIEM/page;
- backup/restore firmado, target distinto, tiempos y decisión de rollback;
- matriz de versiones PostgreSQL/DataHub/IdP/browser realmente probadas.

No guardar DSN, token, prompt, SQL, filas ni schema protegido en el bundle público.

## Parada, rollback y escalado

Parar ante placeholder, imagen mutable, schema inesperado, writer en web/API, TLS/identity
incompletos, egress no previsto, alerta sin destino, restore no verificable o capacidad no medida.
Rollback es retirar tráfico, revocar credenciales afectadas y restaurar el último artefacto/active
pointer aceptado mediante el runbook; nunca editar migrations o documentos inmutables. Escalar a
platform/security owner y mantener NO-GO hasta nueva evidencia.
