---
target: public home page
total_score: 15
max_score: 28
na_heuristics: 7,9,10
p0_count: 0
p1_count: 3
target_identity: "file:C:\\Projects\\FeedBackHubAI\\templates\\feedback\\home.html"
target_fingerprint: "sha256:ed04fee735143a64b0ffa382d4b3c460213fe9217437cb67d43108eab5a5b8c4"
target_path: "C:\\Projects\\FeedBackHubAI\\templates\\feedback\\home.html"
timestamp: 2026-10-29T14-31-04Z
slug: templates-feedback-home-html
---
# Critique: public home page (templates/feedback/home.html)
Method: dual-agent; browser screenshots/overlay unavailable (browser policy errors); detector run against live URL.

Score 15/28 (heuristics 7, 9, 10 n/a). H1 2, H2 1, H3 3, H4 1, H5 3, H6 3, H8 2.

Priority issues:
1. [P1] Developer jargon throughout visible copy (Supabase, input hash, evidence refs, statistics/text/synthesis, gemini-3.6-flash, Spearman ρ without gloss) -> clarify.
2. [P1] Customer-notification loop missing; story stops at STEP 05 -> shape, layout.
3. [P1] Brand name inconsistent: Feedback IQ / FeedBack IQ / Feedback Insight Hub AI / 回饋洞察 AI 平台 -> clarify.
4. [P2] Demo numbers contradict: 1–5 scale vs 平均滿意度 6.04; 價格 34 次 top keyword vs 等候/服務 priority; 103 vs 100 responses -> harden.
5. [P2] CTA hierarchy inverted (查看產品流程 scroll link is primary); 10px/10.5px labels (home.html 136–138, 239–263); 3.9:1 contrast on 示範資料 small (49–50); #bdd2c2 on #133b2b (297); .public-hero defined 4+ times in app.css -> layout, typeset.
Minor: Spearman string repeated 4x; English mono eyebrows; nested cards public-metric-strip/public-stage-list (may be intended).
