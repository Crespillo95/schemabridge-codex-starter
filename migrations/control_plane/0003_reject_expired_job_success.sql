CREATE FUNCTION schemabridge_control.reject_expired_execution_job_success()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, schemabridge_control
AS $$
BEGIN
    IF (
        NEW.status = 'succeeded'
        AND OLD.status IS DISTINCT FROM 'succeeded'
        AND OLD.authorization_expires_at <= clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'expired execution job authorization cannot succeed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION
    schemabridge_control.reject_expired_execution_job_success()
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
    schemabridge_control.reject_expired_execution_job_success()
    TO schemabridge_worker, schemabridge_migrator;

CREATE TRIGGER execution_jobs_authorization_expiry
BEFORE UPDATE ON schemabridge_control.execution_jobs
FOR EACH ROW
WHEN (NEW.status = 'succeeded')
EXECUTE FUNCTION schemabridge_control.reject_expired_execution_job_success();
