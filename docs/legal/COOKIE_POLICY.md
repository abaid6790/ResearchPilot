# Cookie Policy

**Operator:** [OPERATOR: legal name]  **Effective date:** [OPERATOR: effective date]

This lists everything the application sets in your browser. It was written from the code; if a
deployment adds analytics, advertising or other scripts, this policy must be updated.

## Cookies

| Name | Purpose | Lifetime | Notes |
|---|---|---|---|
| `rp_session` | Keeps you signed in and holds the CSRF token that protects forms | Browser session; the server treats the session as expired after `SESSION_LIFETIME_HOURS` (default 12 hours) | First-party, `HttpOnly`, `SameSite=Lax`, and `Secure` when the application runs in production mode |

There is no "remember me" cookie. The session cookie is strictly necessary to sign in, so it is set
without a consent banner. [OPERATOR: confirm with legal review whether your jurisdiction requires
anything further for strictly necessary cookies.]

## Browser storage

| Key | Purpose | Lifetime |
|---|---|---|
| `localStorage` `rp-theme` | Remembers the light/dark choice made with the top-bar toggle in this browser | Until you clear site data; if you save a theme in Settings, the account preference is used instead |

## What we do not use
No advertising cookies, no third-party analytics, no tracking pixels, and no social-media widgets.
Pages load scripts and styles only from the application's own origin.

## Controlling storage
You can clear or block cookies and site data in your browser. Blocking `rp_session` prevents sign-in.

## Points that need legal review
* Whether notices or consent are needed for the storage listed above where you operate.
* Any cookies set by your reverse proxy, hosting platform or load balancer, which this application
  does not control. [OPERATOR: list them or state there are none.]
