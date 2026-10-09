SELECT COALESCE(SUM(mrr), 0) * 12 AS paid_arr
FROM billing.subscriptions
WHERE status = 'active' AND mrr > 0;
