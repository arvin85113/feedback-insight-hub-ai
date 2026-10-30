---
version: 1
slug: "templates-feedback-home-html"
primary_target: "templates/feedback/home.html"
related_targets: ["templates/feedback/case_study.html"]
---

Scope: public homepage (`/`), visitor mode Persuade; the case study page (`/case-study/`) is its Read-mode companion.
Audience: store managers (primary action: log in to the manager workspace) and customers (log in, fill a survey, follow what happened). One page, two modes switched at the top; each mode carries only its own message.
Proof: analysis numbers are computed by the program; AI text is checked number by number against evidence; improvements end with notifying the customer. Demo numbers are labelled as demo data. No invented customers, prices or metrics.
Constraints: plain Traditional Chinese, no developer jargon on this page; public styles must not reach manager pages.

## Direction contract
THESIS: The category-standard SaaS homepage, executed at Stripe's level of finish; it refuses the split-hero-plus-equal-cards template done carelessly, and refuses one generic message for two audiences.
OWN-WORLD: White ground and a cool off-white band, deep navy ink, one confident green for store mode and a warm amber for customer mode; large product visuals with real content, crisp 1px borders, soft offset shadows, Noto Sans TC with heavy display weight.
STORY: A store manager sees one feedback result become one decision and one customer notification, believes the numbers are checked, and logs in. A customer sees that their comment leads to a visible follow-up and logs in to fill a survey.
FIRST VIEWPORT: Nav; a two-option mode switch above a 2–3 line display headline at left; lead and primary button below; at right one large product visual for the active mode (analysis panel for store, phone timeline for customer) over a diagonal colour field tinted by the mode.
FORM: canon (standing exit chosen by the user), benchmark Stripe; seed:4b240b44cf31a201
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
