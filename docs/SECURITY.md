# Security

This page starts with the application-level controls, then covers retrieved and uploaded content.
To report a vulnerability see `docs/legal/SECURITY_DISCLOSURE.md`.

## Application controls

| Area | What the code does |
|---|---|
| Passwords | Werkzeug salted hashes; length limits; equal-time check for unknown users; password change invalidates other sessions (`auth_version`) |
| Email tokens | 256-bit random, only the SHA-256 digest stored, single use, expiring, confirmation page before consuming |
| Sessions | Cookie `rp_session`: HttpOnly, SameSite=Lax, Secure in production, `SESSION_LIFETIME_HOURS` |
| CSRF | Required on every state-changing request; logout is POST-only |
| Headers | Content-Security-Policy (`default-src 'self'`, no inline script or style), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, restrictive `Permissions-Policy`, `Cross-Origin-Opener-Policy: same-origin`, HSTS in production; HTML responses are `no-store` |
| Rate limits | Per client IP on authentication, creation, runs, edits, exports (see `API_DOCUMENTATION.md`); Redis storage required in production |
| Authorisation | Every project resource is loaded through `research/access.py`; foreign ids return 404 |
| Output | Markdown rendered with `bleach` allow-lists; exports sanitised; citation markers and URLs checked before use |
| Secrets | Read from the environment only; startup refuses weak or missing production secrets; logs pass a redaction filter; keys are never sent to the browser |
| Audit | Events record type, time, target and small details, never passwords, tokens, emails or report text |
| Privacy | Provider usage records hold no prompt text; data export and account deletion are in `ACCOUNT_DATA.md` |

## Threat model (summary)
* **Malicious web page or upload** (prompt injection, SSRF, parser exploits): see below.
* **Account takeover:** rate limits, hashed single-use tokens, session invalidation; no multi-factor
  authentication exists.
* **Cross-site attacks:** CSRF tokens, strict CSP, output sanitising.
* **Data exposure to providers:** research text is sent to the configured LLM and search providers by
  design; enable only providers whose terms you accept.
* **Resource abuse:** limits on active runs, per-run budgets, upload and fetch size limits.

## Operating securely
Serve over HTTPS; set `TRUSTED_PROXY_COUNT` correctly; keep the environment file at mode 600; rotate
keys (`API_KEY_ROTATION.md`); keep dependencies updated and review advisories for PyMuPDF,
python-docx, ReportLab, Werkzeug and Flask; back up and encrypt backups; schedule `purge-accounts`.

## Incident response
If a key leaks: revoke it at the provider, replace it, restart, and check usage records. If a
database or server is compromised: rotate `SECRET_KEY` (signs everyone out) and all keys, force
password resets as needed, and check whether notification duties apply where you operate (legal
review). Not provided: intrusion detection, alerting or a formal vulnerability programme.

## Retrieved and uploaded content

Web pages, uploaded files and model output are all **untrusted**. This is what the code does
about it (`research_tools/`), and what it does not.

## SSRF (making the server fetch internal addresses)

`research_tools/net.py` and `fetch.py`:

* only `http` and `https`; no credentials in the URL; ports 80, 443, 8080, 8443 only;
* the host name is resolved at **connection time** and the connection is made to the address that
  was checked, so a name cannot resolve to a public address for the check and a private one for the
  connection (DNS rebinding). If any answer is non-public the fetch is refused;
* refused ranges: loopback, private, link-local (including the cloud metadata address
  169.254.169.254), carrier-grade NAT, multicast, reserved, unspecified, benchmarking, 6to4, Teredo,
  NAT64, and IPv4-mapped IPv6 forms of those;
* redirects are followed by hand (at most 5) and every hop goes through the same checks;
* proxy environment variables are ignored (a proxy would resolve names itself);
* the body is limited after decompression (default 5 MB), the whole fetch has a time budget, and
  only HTML, plain text and PDF responses are accepted.

There is no setting that turns these checks off. The tests inject a permissive policy only through
a constructor argument that the application never uses.

## Prompt injection

A page can contain text such as "ignore previous instructions". Defences, in order of importance:

1. Architecture: agents that read the web have no tools that act (no shell, no file access, no
   arbitrary HTTP); their output is validated against a schema before use (Phase 6).
2. `untrusted.wrap_untrusted` frames content as data inside a prompt with a random boundary.
3. `untrusted.sanitize_text` removes control, zero-width and bidirectional characters.
4. `untrusted.injection_signals` flags suspicious phrasing on a source as a warning. The text is
   kept (evidence is not silently edited) and the warning is shown.

None of this makes injection impossible. A model can still be influenced by text it reads, which
is why claims are checked against stored excerpts and the report lists limitations.

## Uploads

Files are validated by content, not by name or browser-reported type: PDFs must start with
`%PDF-`, DOCX must be a zip with `word/document.xml` (unpacked size and member count are limited),
text must not contain NUL bytes, and the extension must match. Password-protected PDFs and PDFs
over 300 pages (extra pages ignored) are handled explicitly. Files are read in memory and **never
written to disk**; only extracted text (max 500,000 characters) is stored, and it is deleted with
the project. Limits: 5 files, 10 MB each, and the overall request limit `MAX_CONTENT_LENGTH_MB`.

## Not done

* robots.txt is not consulted. The fetcher identifies itself as `ResearchPilotBot`.
* No malware scanning of uploads. Text extraction libraries (PyMuPDF, python-docx) have had
  vulnerabilities; keep them updated.
* JavaScript is not executed, so some pages return little text.
* The link status of a source means "the URL answered", never "the source supports the claim".
