SELECT
  CAST(DATE_TRUNC('DAY', r0.registration_date) AS DATE) AS registration_date,
  COUNT(
    DISTINCT CASE
      WHEN TRIM(r0.customer_id) ~ %s
      THEN COALESCE(NULLIF(TRIM(LEADING '0' FROM TRIM(r0.customer_id)), ''), '0')
      ELSE NULL
    END
  ) AS secondary_holder_customers
FROM crm.customers AS r0
INNER JOIN bank.account_holders AS r1
  ON CASE
    WHEN TRIM(r0.customer_id) ~ %s
    THEN COALESCE(NULLIF(TRIM(LEADING '0' FROM TRIM(r0.customer_id)), ''), '0')
    ELSE NULL
  END = CASE
    WHEN r1.gf_customer_id IS NULL
    OR r1.gf_customer_id IN (
      CAST('NaN' AS DOUBLE PRECISION),
      CAST('Infinity' AS DOUBLE PRECISION),
      CAST('-Infinity' AS DOUBLE PRECISION)
    )
    OR r1.gf_customer_id <> TRUNC(r1.gf_customer_id)
    OR r1.gf_customer_id > 9007199254740991
    OR r1.gf_customer_id < -9007199254740991
    THEN NULL
    ELSE CAST(CAST(r1.gf_customer_id AS BIGINT) AS TEXT)
  END
WHERE
  CASE r1.holder_type
    WHEN %s
    THEN %s
    WHEN %s
    THEN %s
    WHEN %s
    THEN %s
    WHEN %s
    THEN %s
    ELSE NULL
  END = %s
GROUP BY
  CAST(DATE_TRUNC('DAY', r0.registration_date) AS DATE)
ORDER BY
  CAST(DATE_TRUNC('DAY', r0.registration_date) AS DATE) ASC
LIMIT 500
