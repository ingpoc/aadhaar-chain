-- Seed Test Mode Seller AgentGuard mandate for the preprod Auth0 owner of ondcseller.
-- Mirrors SellerAgentGuardOrchestrator.ensure_agent (active mandate + activate on agent).
-- Separate statements required: PG data-modifying CTEs share one snapshot.

INSERT INTO agentguard_agents (
    agent_id,
    principal_id,
    role,
    status,
    payload
)
SELECT
    'agent_seller_e24723ffb9d2636d0bea',
    'principal:auth0:google-oauth2:109432510636331667287',
    'seller',
    'active',
    '{"name":"Seller commerce agent"}'::jsonb
WHERE EXISTS (
    SELECT 1
    FROM commerce_seller_staff
    WHERE seller_id = 'ondcseller'
      AND member_principal_id = 'principal:auth0:google-oauth2:109432510636331667287'
      AND role = 'owner'
      AND status = 'active'
)
ON CONFLICT (principal_id, agent_id) DO UPDATE SET
    status = 'active',
    updated_at = NOW()
WHERE agentguard_agents.status <> 'revoked';

INSERT INTO agentguard_mandate_versions (
    mandate_id,
    version,
    principal_id,
    agent_id,
    status,
    payload
)
SELECT
    'mandate_seller_e24723ffb9d2636d0bea',
    1,
    'principal:auth0:google-oauth2:109432510636331667287',
    'agent_seller_e24723ffb9d2636d0bea',
    'active',
    jsonb_build_object(
        'allowed_actions', jsonb_build_array(
            'seller.catalog.archive',
            'seller.catalog.publish',
            'seller.fulfilment.commit',
            'seller.inventory.commit',
            'seller.order.accept',
            'seller.order.reject',
            'seller.price.change',
            'seller.refund.issue',
            'seller.remedy.promise'
        ),
        'auto_approve_max_inr', jsonb_build_object('seller.refund.issue', 5000),
        'currency', 'INR'
    )
WHERE EXISTS (
    SELECT 1
    FROM agentguard_agents
    WHERE principal_id = 'principal:auth0:google-oauth2:109432510636331667287'
      AND agent_id = 'agent_seller_e24723ffb9d2636d0bea'
      AND status <> 'revoked'
)
  AND NOT EXISTS (
    SELECT 1
    FROM agentguard_mandate_versions
    WHERE mandate_id = 'mandate_seller_e24723ffb9d2636d0bea'
      AND version = 1
);

UPDATE agentguard_agents
SET
    current_mandate_id = 'mandate_seller_e24723ffb9d2636d0bea',
    current_mandate_version = 1,
    updated_at = NOW()
WHERE principal_id = 'principal:auth0:google-oauth2:109432510636331667287'
  AND agent_id = 'agent_seller_e24723ffb9d2636d0bea'
  AND status <> 'revoked'
  AND EXISTS (
    SELECT 1
    FROM agentguard_mandate_versions
    WHERE mandate_id = 'mandate_seller_e24723ffb9d2636d0bea'
      AND version = 1
      AND status = 'active'
  );
