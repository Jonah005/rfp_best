# Penetration Testing Methodology

Approved by the Cybranytech Technical Director, revision 4, 20 January 2026.

## Web application testing

Assessment follows the OWASP Testing Guide. Each application is tested for
broken access control, injection, authentication and session management
weaknesses, insecure direct object references, server-side request forgery,
cryptographic failures and business-logic abuse. Testing is performed with both
unauthenticated and authenticated roles supplied by the customer.

## Application programming interface testing

API assessment covers authentication and authorisation on every exposed method,
object and function level access control, rate limiting, mass assignment, input
validation, injection through structured payloads, and data exposure in
responses. Specifications are reviewed where provided and endpoints are also
enumerated independently.

## Reporting and retesting

Findings are rated by impact and likelihood, evidenced with reproduction steps,
and delivered in a consolidated report. A remediation workshop is held with the
customer's technical team. Retesting confirms closure of each finding and is
reported separately.
