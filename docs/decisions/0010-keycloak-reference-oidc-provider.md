# ADR 0010: Select Keycloak as the reference OIDC provider

- **Status:** Accepted for the reference deployment profile; production deployment not yet authorized
- **Date:** 2026-11-25
- **Related:** [ADR 0009 — provider-neutral gRPC identity and resource authorization](0009-provider-neutral-grpc-identity-and-resource-authorization.md), [Issue #73](https://github.com/mojtaba-arvin/video-service/issues/73), [security and webhook release runbook](../operations/security-and-webhook-receiver-release-runbook.md)

## Context

The service already has a provider-neutral OIDC/JWKS verifier. Its configured trust contract requires an exact HTTPS issuer, a dedicated API audience, a trusted HTTPS JWKS URI, asymmetric JWT signing (currently RS256), a stable subject claim (sub), one non-empty tenant identifier (tenant_id), and normalized scope and roles claims.

The repository's documented deployment foundation is portable Docker Compose with self-managed Redis, RabbitMQ, and S3-compatible storage. No production hosting platform, domain, identity tenant, or deployment workflow has been selected. The identity provider should therefore avoid binding the domain model to a cloud vendor while allowing a self-managed reference deployment and explicit B2B organization membership.

## Decision

Select **Keycloak with its Organizations feature (26.6 or later)** as the reference OIDC provider for development, staging, and the first production integration.

Use a dedicated realm for the service deployment and a dedicated OIDC client according to the actual caller type. Register a distinct resource audience for the gRPC API (for example, the logical identifier **video-service**); never accept a generic account-console audience.

Use Keycloak Organizations for customer/tenant membership. Request the built-in **organization** scope in a single-organization context. For users who belong to multiple organizations, select one explicit organization using **organization:<alias>**; never use **organization:*** for this service's tenant-scoped API tokens.

Configure the Organization Membership mapper to include the immutable organization ID in access tokens. The service must receive exactly one selected tenant as the top-level string claim **tenant_id**. Keycloak's native organization representation is an **organization** object keyed by organization alias; it is not itself the current service's tenant_id string. Before production, implement and test an explicit mapper/adapter that emits the selected organization's immutable ID as tenant_id. Do not point OIDC_TENANT_CLAIM at the nested object, use a mutable organization alias as tenant identity, or accept a token containing ambiguous multiple organizations.

Map the API permissions to the service's existing claims:
- **sub**: stable Keycloak subject;
- **tenant_id**: selected immutable organization ID, emitted as a top-level string;
- **scope**: space-delimited OAuth scopes, including only permissions requested and granted to that client;
- **roles**: a flat string list of application roles needed by the service, including **video.tenant.quota.admin** only for named operator identities.

Use RS256 initially, publish keys through Keycloak's OIDC discovery/JWKS endpoints, and retain the verifier's bounded JWKS cache and fail-closed behavior. Key rotation must be tested against real access tokens before release. gRPC TLS remains independently mandatory.

## Why Keycloak

1. **Matches the deployment shape.** The repository is built around portable, self-managed infrastructure rather than an already-selected cloud platform. Keycloak can be operated alongside that stack without making the service depend on one cloud IAM.
2. **Supports the required token contract.** Protocol mappers can place user attributes, roles, audience and organization membership into access tokens. The Organizations feature supports selecting one organization in the OIDC request and includes its immutable ID when configured.
3. **Supports B2B tenant modeling.** Organizations represent customer tenants; membership and organization-scoped groups avoid inventing tenant identity from email domains or caller-supplied metadata.
4. **Keeps the service provider-neutral.** No Keycloak SDK or admin API dependency is added to the request path. The service continues to validate standard OIDC JWTs against configured issuer, audience and JWKS values.

Auth0 and ZITADEL remain valid alternatives if a managed identity service becomes a firm deployment requirement. They are not selected now because no managed-identity hosting requirement or cloud platform exists in the repository's verified deployment context. This is a reversible operational choice, not a change to ADR 0009's provider-neutral application contract.

## Operational constraints

- Run Keycloak as a separately managed identity service with a supported persistent database, durable backups, TLS, stable hostname/issuer, monitoring, upgrade/patch policy and tested restore. Do not use the development in-memory/dev database profile in production.
- Do not deploy Keycloak in the video worker container or couple its lifecycle to FFmpeg workers.
- Do not add Keycloak secrets, signing keys, client secrets or exported production realm credentials to Git.
- Do not create a GitHub Environment or GitHub Actions deployment workflow until the actual deployment target and runtime configuration mechanism are selected.
- Do not invent issuer, audience, JWKS URI, realm, hostname, credentials or tenant IDs. Obtain exact values from the provisioned Keycloak instance and its discovery document.
- Do not enable tenant quota enforcement or any security feature flag merely because an IdP is selected. Issue #73's operator, tenant inventory, receiver replay protection and release-evidence gates remain mandatory.

## Acceptance gates before production

1. Provision a persistent, TLS-protected Keycloak instance and record its exact issuer/discovery URL.
2. Create the dedicated realm, API client, resource audience, organization scope and least-privilege client roles.
3. Implement the selected-organization-to-tenant_id mapping and verify that a multi-organization user receives exactly one tenant per access token.
4. Verify access tokens contain iss, aud, exp, sub, tenant_id, scope, and roles in the exact shapes accepted by the service.
5. Verify wrong issuer/audience, missing or ambiguous tenant, missing subject, expired token, unsupported algorithm, unknown key and JWKS outage fail closed.
6. Verify role/scope grants and revocations for normal clients, tenant users, and named quota operators.
7. Verify signing-key rotation and TLS certificate rotation.
8. Store only the sanitized issuer, audience, JWKS URI, realm/client identifiers and test evidence in the release record; never store tokens or secrets.
9. Enable production OIDC only through a separately reviewed deployment change after all Issue #73 gates are satisfied.

## Consequences

- Keycloak is the documented reference IdP, but application code remains generic OIDC.
- Production deployment is still blocked until an actual instance and hosting/runtime configuration exist.
- The current verifier's simple tenant_id contract needs an explicit, tested mapping from Keycloak's selected organization context.
- Operating an IdP becomes an availability and security responsibility; persistent storage, backups, patching, TLS and restore testing are non-optional.
