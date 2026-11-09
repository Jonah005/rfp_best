# Secure Development Policy

Owner: Head of Engineering. Approved: 12 February 2025. Next review: February 2027.

## Scope

Applies to every web application Wadi Lumen develops or maintains.

## Rules

1. All input from users is validated on the server, whatever the browser has
   already checked.
2. All output written to a page is encoded for its context, to prevent
   cross-site scripting. Razor encoding is kept on by default and may only be
   turned off with a reviewer's approval.
3. Database access uses parameterised queries or Entity Framework. String
   concatenation into SQL is not allowed.
4. Redirects only go to addresses on an approved list held in configuration.
5. Passwords are stored as salted hashes using PBKDF2. They are never stored in
   a readable form.
6. Other personal data is encrypted at rest only where the client's
   requirements specify it. By default, personal data other than passwords is
   stored in SQL Server without column-level encryption and protected by
   database access controls.
7. Every release is checked against the OWASP Top 10 by a developer who did not
   write the code.
8. All sites are served over HTTPS only.

## Third-party components

We use open-source libraries under MIT or Apache 2.0 licences. We do not buy
commercial third-party controls on the client's behalf; where a project needs
one, the client purchases the licence in its own name.
