-- Bind the existing PreProd store to its Auth0 owner after the Neon cutover.

INSERT INTO commerce_seller_staff (
    staff_id,
    seller_id,
    member_principal_id,
    display_name,
    role,
    status
)
SELECT
    'staff_92435bee79aa6477',
    'ondcseller',
    'principal:auth0:google-oauth2:109432510636331667287',
    'Store owner',
    'owner',
    'active'
WHERE EXISTS (
    SELECT 1
    FROM commerce_seller_stores
    WHERE seller_id = 'ondcseller' AND status = 'ready'
)
ON CONFLICT (seller_id, member_principal_id) DO UPDATE SET
    role = 'owner',
    status = 'active',
    version = commerce_seller_staff.version + 1,
    updated_at = NOW()
WHERE commerce_seller_staff.role <> 'owner'
   OR commerce_seller_staff.status <> 'active';
