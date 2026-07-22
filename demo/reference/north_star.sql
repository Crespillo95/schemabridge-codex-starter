-- Ground-truth fixture only. Production code must compile equivalent SQL from
-- approved semantic mappings and join contracts; it must never copy this file.
WITH normalized_customers AS (
    SELECT
        COALESCE(NULLIF(LTRIM(customer_id, '0'), ''), '0') AS customer_key,
        registration_date
    FROM crm.customers
),
classified_secondary_holders AS (
    SELECT
        CASE
            WHEN gf_customer_id IS NULL THEN NULL
            WHEN gf_customer_id IN (
                'NaN'::DOUBLE PRECISION,
                'Infinity'::DOUBLE PRECISION,
                '-Infinity'::DOUBLE PRECISION
            ) THEN NULL
            WHEN gf_customer_id <> TRUNC(gf_customer_id) THEN NULL
            ELSE CAST(gf_customer_id AS BIGINT)::TEXT
        END AS customer_key
    FROM bank.account_holders
    WHERE holder_type IN ('SECONDARY', '2', 'CO_HOLDER')
),
normalized_secondary_holders AS (
    SELECT customer_key
    FROM classified_secondary_holders
    WHERE customer_key IS NOT NULL
)
SELECT
    c.registration_date,
    COUNT(DISTINCT c.customer_key) AS secondary_holder_customers
FROM normalized_customers AS c
INNER JOIN normalized_secondary_holders AS h
    ON c.customer_key = h.customer_key
GROUP BY c.registration_date
ORDER BY c.registration_date;

-- Expected rows:
-- 2026-01-01 | 2
-- 2026-01-02 | 1
-- 2026-01-03 | 1
