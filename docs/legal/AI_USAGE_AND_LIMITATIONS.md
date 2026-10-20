# AI Usage and Limitations

**Operator:** [OPERATOR: legal name]  **Effective date:** [OPERATOR: effective date]

ResearchPilot AI uses large language models and web search to draft reports. This page explains what
that means in practice so you can use the output sensibly.

## How a report is made
1. A model plans subtopics and search queries from your question.
2. Code searches the web (Tavily), then fetches pages and reads uploaded documents. Selected passages
   are stored as verbatim excerpts.
3. A model extracts claims from numbered excerpts; each claim must cite excerpt identifiers.
4. A model writes each section using only the claims provided, with citation markers.
5. A second model call reviews the draft; major issues trigger a revision, up to a set number of rounds.
6. Code checks citations and quotations, and a model judges whether each cited excerpt supports its
   claim. The *Limitations* and *References* sections are generated from database facts, not by a model.

Prompts, excerpts and drafts are sent to the AI providers enabled by the operator (see the Privacy
Policy).

## What can go wrong
* **Wrong or invented statements.** Models can state falsehoods fluently. The design limits this by
  tying claims to stored excerpts and removing citations to sources that were not provided, but it
  cannot guarantee that every statement is true or that a cited source supports it.
* **Source quality and bias.** Search results can be inaccurate, outdated, commercial or biased. A
  source being cited is not an endorsement. The software does not judge truth beyond its checks.
* **Missing material.** Pages that need JavaScript, paywalled or blocked pages, scanned documents
  (there is no OCR) and robots.txt rules are not specially handled; coverage can be incomplete.
* **Prompt injection.** Web pages and uploads can contain text that tries to steer a model. Content is
  treated as untrusted data and flagged when it looks like instructions, which reduces but does not
  remove the risk.
* **Language and layout.** PDF export cannot render some scripts (such as Arabic or Urdu); Word and
  HTML exports can.

## What the quality checks mean
Each report gets scores from fixed, transparent rules (for example whether claims have citations and
whether citations resolve). Items that cannot be measured are shown as "not assessed". Some model-written
opinions may be shown separately; they never change whether a report is marked as checked.
A report marked **checked** has passed those automated gates. That is **not** proof of accuracy and is
not human review. Exports from reports that are not fully checked carry a "NOT FULLY VERIFIED" notice.
Editing a report clears the checked mark until you re-run the checks.

## Your responsibilities
Open the cited sources for any claim that matters, treat reports as drafts, do not present AI-assisted
text as independently verified, and do not use it as professional advice or for decisions where an error
could cause serious harm.

## Points that need legal review
* Disclosure duties about AI-generated content in your jurisdiction or sector.
* Any sector-specific restrictions (for example health, legal, financial, education).
