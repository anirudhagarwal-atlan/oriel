WITH enabled_links AS (
  SELECT * FROM directory.workspace_map WHERE enabled
), mapping_issues AS (
  SELECT 'ambiguous_workspace' AS issue, workspace_ref AS source_key
  FROM enabled_links GROUP BY workspace_ref HAVING COUNT(*) > 1
  UNION ALL
  SELECT DISTINCT 'missing_workspace', e.workspace_ref
  FROM telemetry.events e
  WHERE NOT EXISTS (SELECT 1 FROM enabled_links m WHERE m.workspace_ref = e.workspace_ref)
  UNION ALL
  SELECT DISTINCT 'unknown_customer', m.customer_key
  FROM enabled_links m
  WHERE NOT EXISTS (SELECT 1 FROM sales.seats c WHERE c.organization_id = m.customer_key)
  UNION ALL
  SELECT 'duplicate_customer', organization_id
  FROM sales.seats GROUP BY organization_id HAVING COUNT(*) > 1
  UNION ALL
  SELECT 'unmapped_customer', c.organization_id
  FROM sales.seats c
  WHERE c.contract_state = 'active' AND c.entity_kind = 'customer'
    AND NOT EXISTS (SELECT 1 FROM enabled_links m WHERE m.customer_key = c.organization_id)
)
SELECT issue, source_key FROM mapping_issues ORDER BY issue, source_key
