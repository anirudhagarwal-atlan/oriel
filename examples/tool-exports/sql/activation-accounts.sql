WITH enabled_links AS (
  SELECT * FROM directory.workspace_map WHERE enabled
), mapping_issues AS (
  SELECT workspace_ref AS source_key
  FROM enabled_links GROUP BY workspace_ref HAVING COUNT(*) > 1
  UNION ALL
  SELECT DISTINCT e.workspace_ref FROM telemetry.events e
  WHERE NOT EXISTS (SELECT 1 FROM enabled_links m WHERE m.workspace_ref = e.workspace_ref)
  UNION ALL
  SELECT DISTINCT m.customer_key FROM enabled_links m
  WHERE NOT EXISTS (SELECT 1 FROM sales.seats c WHERE c.organization_id = m.customer_key)
  UNION ALL
  SELECT organization_id FROM sales.seats GROUP BY organization_id HAVING COUNT(*) > 1
  UNION ALL
  SELECT c.organization_id FROM sales.seats c
  WHERE c.contract_state = 'active' AND c.entity_kind = 'customer'
    AND NOT EXISTS (SELECT 1 FROM enabled_links m WHERE m.customer_key = c.organization_id)
), activators AS (
  SELECT m.customer_key, COUNT(DISTINCT e.member_ref) AS activated_members
  FROM telemetry.events e JOIN enabled_links m ON m.workspace_ref = e.workspace_ref
  WHERE e.feature_key = 'publish_report' AND e.member_ref IS NOT NULL
    AND e.occurred_at >= TIMESTAMP '2026-09-24 00:00:00'
    AND e.occurred_at < TIMESTAMP '2026-10-08 00:00:00'
  GROUP BY m.customer_key
), account_activation AS (
  SELECT c.organization_id, c.licensed_seats,
    COALESCE(a.activated_members, 0) AS activated_members,
    c.licensed_seats - COALESCE(a.activated_members, 0) AS unused_seats
  FROM sales.seats c LEFT JOIN activators a ON a.customer_key = c.organization_id
  WHERE c.contract_state = 'active' AND c.entity_kind = 'customer' AND c.licensed_seats > 0
)
SELECT organization_id, licensed_seats, activated_members, unused_seats
FROM account_activation
WHERE CASE WHEN EXISTS (SELECT 1 FROM mapping_issues)
           THEN error('Resolve identity mapping before computing activation.')
           ELSE activated_members * 2 < licensed_seats END
ORDER BY organization_id
