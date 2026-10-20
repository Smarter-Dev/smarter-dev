-- The moderation coordinator's shadow, compared with the legacy actors (#239).
--
-- Read-only: run it as a Job against production (postgres:17-alpine, the
-- database URL from smarter-dev-secrets), never from a serving pod. It only
-- reads moderation_shadow_decisions and usage_cost_rows, inside a READ ONLY
-- transaction that is rolled back.
--
--   psql "$DB" -v ON_ERROR_STOP=1 -v hours=24 -f moderation_shadow_summary.sql
--
-- Rows whose legacy side is not compared yet (younger than ~3 minutes) are
-- counted as pending and left out of the comparison.

BEGIN READ ONLY;
SET search_path TO skrift;
\set since '(now() - (:hours || '' hours'')::interval)'

-- 1. Coverage
SELECT count(*) AS revisions,
       count(*) FILTER (WHERE is_edit) AS edits,
       count(*) FILTER (WHERE error IS NOT NULL) AS not_judged,
       count(*) FILTER (WHERE legacy_checked_at IS NULL) AS comparison_pending,
       min(created_at) AS first, max(created_at) AS last
  FROM moderation_shadow_decisions WHERE created_at >= :since;

-- 2. Agreement with the legacy actors
SELECT agreement, count(*)
  FROM moderation_shadow_decisions
 WHERE created_at >= :since AND legacy_checked_at IS NOT NULL
 GROUP BY 1 ORDER BY 2 DESC;

-- 3. Strongest action: shadow (rows) by legacy (columns)
SELECT coalesce(strongest_action, 'none') AS shadow,
       count(*) FILTER (WHERE legacy_strongest_action IS NULL) AS legacy_none,
       count(*) FILTER (WHERE legacy_strongest_action = 'delete') AS legacy_delete,
       count(*) FILTER (WHERE legacy_strongest_action = 'warn') AS legacy_warn,
       count(*) FILTER (WHERE legacy_strongest_action = 'timeout') AS legacy_timeout,
       count(*) FILTER (WHERE legacy_strongest_action = 'kick') AS legacy_kick,
       count(*) FILTER (WHERE legacy_strongest_action = 'ban') AS legacy_ban
  FROM moderation_shadow_decisions
 WHERE created_at >= :since AND legacy_checked_at IS NOT NULL
 GROUP BY 1 ORDER BY 1;

-- 4. Every disagreement, to review one by one (ids and rule names only)
SELECT created_at, channel_id, message_id, revision, author_id,
       coalesce(strongest_action, 'none') AS shadow, coalesce(legacy_strongest_action, 'none') AS legacy,
       agreement, target_ids, legacy -> 'acted_on' AS legacy_targets,
       legacy -> 'actors' AS legacy_actors, rules, staff_excluded,
       decision -> 'review' AS review, classifier -> 'scam_outcome' AS scam_outcome
  FROM moderation_shadow_decisions
 WHERE created_at >= :since
   AND agreement IN ('shadow_stronger', 'shadow_weaker', 'different_targets')
 ORDER BY created_at;

-- 5. Rules that fired
SELECT split_part(rule.value #>> '{}', ':', 1) AS rule, count(*)
  FROM moderation_shadow_decisions, jsonb_array_elements(rules::jsonb) AS rule
 WHERE created_at >= :since
 GROUP BY 1 ORDER BY 2 DESC;

-- 6. Classifier requests, backend, outcome and spend
SELECT classifier ->> 'backend' AS backend, classifier ->> 'outcome' AS outcome,
       classifier ->> 'refused_by' AS refused_by,
       count(*) AS revisions,
       sum((classifier ->> 'requests')::int) AS requests,
       sum((classifier ->> 'input_tokens')::int) AS input_tokens,
       sum((classifier ->> 'output_tokens')::int) AS output_tokens,
       round(sum((classifier ->> 'cost_usd')::numeric), 6) AS cost_usd,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY (classifier ->> 'latency_ms')::int) AS p50_ms,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY (classifier ->> 'latency_ms')::int) AS p95_ms
  FROM moderation_shadow_decisions
 WHERE created_at >= :since AND classifier ->> 'backend' IS NOT NULL
 GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- 7. Each label's outcome: how often it came back unknown, and why
SELECT CASE WHEN label.key LIKE 'participant:%' THEN 'participant:*' ELSE label.key END AS label,
       label.value ->> 'outcome' AS outcome,
       count(*) AS n,
       count(*) FILTER (WHERE (label.value ->> 'answer')::boolean) AS yes
  FROM moderation_shadow_decisions, jsonb_each(labels::jsonb) AS label
 WHERE created_at >= :since
 GROUP BY 1, 2 ORDER BY 1, 2;

-- 8. Labels answered from an earlier copy's request (no new request)
SELECT count(*) AS revisions_reusing_a_verdict
  FROM moderation_shadow_decisions
 WHERE created_at >= :since
   AND jsonb_array_length(coalesce(classifier::jsonb -> 'reused_labels', '[]'::jsonb)) > 0;

-- 9. Provisional timeouts (high-signal scams) and what became of them
SELECT provisional ->> 'resolution' AS resolution, count(*),
       percentile_disc(0.5) WITHIN GROUP (ORDER BY (provisional ->> 'after_ms')::int) AS p50_ms_to_hold
  FROM moderation_shadow_decisions
 WHERE created_at >= :since AND provisional IS NOT NULL
 GROUP BY 1;

-- 10. Staff the rules would have acted on, and the decision latency
SELECT count(*) FILTER (WHERE staff_excluded::text NOT IN ('{}', 'null')) AS with_staff_excluded,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_decision_ms,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_decision_ms,
       max(latency_ms) AS max_decision_ms
  FROM moderation_shadow_decisions WHERE created_at >= :since AND error IS NULL;

-- 11. The ledger's view of the same spend
SELECT model_id, count(*) AS operations, sum(input_tokens) AS input_tokens,
       sum(output_tokens) AS output_tokens, round(sum(cost_usd), 6) AS cost_usd
  FROM usage_cost_rows
 WHERE operation_type = 'moderation_shadow_classifier' AND created_at >= :since
 GROUP BY 1;

-- 12. Was the live scam-banner the script the scam gate was ported from?
SELECT policy ->> 'scam_banner_matches_reference' AS matches_reference, count(*)
  FROM moderation_shadow_decisions WHERE created_at >= :since
 GROUP BY 1;

ROLLBACK;
