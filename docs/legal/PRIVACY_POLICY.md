# Privacy Policy

**Operator:** [OPERATOR: legal name of the person or organisation running this service]
**Contact for privacy questions:** [OPERATOR: privacy contact email address]
**Jurisdiction / governing law:** [OPERATOR: country or state whose law applies]
**Effective date:** [OPERATOR: effective date]

This policy explains what information ResearchPilot AI processes, why, and who else receives it. It
describes the software as built. It is not a statement that the service complies with any particular
law; see "Points that need legal review" at the end.

## 1. Information we store

**Account.** Email address, display name, a password hash (the password itself is never stored),
whether the email is verified, theme and notification preferences, default report settings, and the
dates of sign-up, last sign-in and any deletion request.

**Your research content.** Projects (title, question, settings), the text extracted from documents you
upload (the original uploaded file is not kept), research runs with progress events, the web sources
found, short excerpts copied from those sources, claims extracted from them, reports with sections,
edit history, and quality-check results.

**Operational records.**
* *Provider usage records:* which AI provider and model was called, timings, token counts, and
  outcome. They do not contain prompts or responses.
* *Audit events:* type, time, a target identifier and small details (for example an export format).
  They hold no email address or report text.
* *Security tokens:* single-use tokens for email verification and password reset (stored hashed).
* *Rate-limit counters* kept by the application to slow abusive requests.
* *Server logs:* request and error logs written by the application and the web server. The
  application masks API keys and similar secrets in its own log output. [OPERATOR: describe your
  web-server, proxy and hosting logs and how long you keep them.]

## 2. Why we process it
To provide the service you asked for (sign-in, running research, showing and exporting reports), to
send account emails, to keep the service secure and prevent abuse, and to fix faults.
[OPERATOR: state the legal bases you rely on where the law requires them.]

## 3. Who else receives research content

ResearchPilot AI does **not** keep your research on the server only. Running research sends data to
external services chosen by the operator through configuration:

* **AI model providers** (the code supports Google Gemini, Groq and OpenRouter). To plan, analyse,
  write and check a report, the application sends them your research question, settings, excerpts
  retrieved from web pages and text from documents you uploaded, and drafted report text. They
  process it under their own terms and privacy policies.
* **Search provider** (Tavily). Search queries derived from your question are sent to it.
* **Websites.** The application fetches public web pages directly from the server to read sources.
  Those sites can see the server's IP address and the request's user agent (`ResearchPilotBot`).
* **Email delivery** (SMTP server chosen by the operator) for account emails and, if you opt in,
  finish notifications. Finish emails contain the project title, status and a link, not report text.
* **Hosting and infrastructure** providers used by the operator. [OPERATOR: list your hosting,
  database, backup and email providers, and the countries where they process data.]

Do not put into a project anything you are not allowed or willing to share with those providers.
The operator selects which providers are enabled: [OPERATOR: list the providers enabled in this
deployment and link to their policies]. We do not sell personal data and do not use your research
content to train models ourselves; [OPERATOR: confirm, and check each provider's terms on training and
retention for the plan and keys you use].

## 4. Cookies and similar storage
See the Cookie Policy. In short: one session cookie, one browser-storage entry for the theme, and no
advertising or analytics cookies.

## 5. How long we keep it
See the Data Retention Policy. You can delete a project at any time, export your data, and delete your
account; deletion is not instantaneous in backups or at external providers.

## 6. Your choices
Within Settings you can: edit your profile, change your password, set preferences, download a ZIP of
your data, delete projects, and request account deletion (with a grace period during which you can
cancel). [OPERATOR: describe how people can exercise any other rights the law gives them, and
how you verify requests.]

## 7. Security
Passwords are hashed, sessions use HTTP-only cookies, forms use CSRF protection, and secrets are kept
out of source code and logs. No system is perfectly secure; see the Security Disclosure document.

## 8. Children
The service is not designed for children. [OPERATOR: set the minimum age and what you do if you learn a
younger person has registered.]

## 9. International transfers
External providers may process data in other countries. [OPERATOR: describe transfers and any
safeguards you rely on.]

## 10. Changes
[OPERATOR: describe how you will announce changes to this policy.]

## Points that need legal review
* Legal bases, controller/processor roles and any required records of processing.
* Data-subject rights procedures and response times in your jurisdiction.
* Contracts or terms with each AI, search, email and hosting provider; transfer mechanisms.
* Whether a data-protection impact assessment, representative or registration is required.
* Age limits and consent rules for your audience.
