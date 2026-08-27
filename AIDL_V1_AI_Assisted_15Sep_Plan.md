# AIDL — Version 1 Plan (AI-Assisted: Cursor + Claude)

**Source:** [AIDL Feature List — Version Roadmap](https://docs.google.com/spreadsheets/d/1qXLcQqnFqlg3UxRZt_aqNMNlSzOi481SUuJDWOw4uh8/edit?gid=0#gid=0)  
**Planning date:** 25 August 2026  
**Target date:** **15 September 2026**  
**Working days left:** ~**15** (25 Aug → 15 Sep)  
**Team:** 1 Backend + 1 Frontend, both using **Cursor + Claude** for implementation  

---

## 1. How AI changes the timeline

### What Cursor + Claude speeds up a lot
- Boilerplate APIs, models, serializers, CRUD  
- Vue pages, forms, routing, basic UI  
- Refactors, docs, examples  
- Repeating patterns once one feature exists  

### What AI does **not** remove
- Azure / Teams admin consent and redirect debugging  
- Real content writing (curriculum text, questions)  
- Design / product decisions  
- End-to-end QA on production  
- Waiting on deploy / FE–BE integration bugs  
- Security review of auth and billing  

### Effort rule used in this doc

| Work type | Without AI | With Cursor + Claude | Effect |
|-----------|------------|----------------------|--------|
| Pure coding (CRUD, screens, known APIs) | 100% | ~40–50% | **~2–2.5× faster** |
| Integration (Teams OAuth, cards, Stripe) | 100% | ~65–75% | **~1.3–1.5× faster** |
| Content / Azure / human QA / meetings | 100% | ~90–100% | **almost same** |

**Net for this project:** about **~1.7× overall faster** than classic estimates (not 5×).

---

## 2. Target 15 Sep — AI-assisted capacity

| Role | Working days available | Effective coding output (AI) | Notes |
|------|------------------------|------------------------------|--------|
| Backend + Cursor/Claude | 15 days | ~**22–25 classic-days** of code | Still 15 calendar days |
| Frontend + Cursor/Claude | 15 days | ~**22–25 classic-days** of code | Same |

So **15 Sep is realistic** for the **V1 slice** below if the team stays focused and production auth stays green.

---

## 3. What ships by 15 September (IN SCOPE — AI-assisted)

End-to-end demo:

> **Teams login → short learning → quiz → digital licence → public verify**

| # | Feature (from spreadsheet) | Backend (AI-assisted days) | Frontend (AI-assisted days) |
|---|----------------------------|----------------------------|-----------------------------|
| 1 | Teams / Microsoft auth harden + prod | 1–1.5 | 1–1.5 |
| 2 | Core curriculum (3–5 units, seeded) | 1.5–2 | 2–2.5 |
| 3 | Simple sequential unlock (light drip) | 1 | 1 |
| 4 | Question bank + one assessment | 1.5–2 | 1.5–2 |
| 5 | Pass threshold (single %) | 0.5 | 0.5 |
| 6 | Licence issuance (one class) | 1.5–2 | 1.5–2 |
| 7 | Public verify URL (+ QR if time) | 1–1.5 | 1–1.5 |
| 8 | Free/consumer path reuse (optional) | 0.5 | 0.5 |
| 9 | Deploy + E2E smoke QA | 1 | 1 |
| | **Total** | **~10–13 days** | **~10–13 days** |

**Fits in 15 working days** with ~2 days buffer for bugs / Azure / content.

### Cuts if behind (order)
1. QR (keep verify link only)  
2. 5 units → 3 units  
3. Skip consumer register (Teams login only)  
4. Skip auto-open Teams window  

---

## 4. Week-by-week (AI-assisted) — 25 Aug → 15 Sep

### Week 1 (25–31 Aug) — Auth + Learn
| Backend (Cursor) | Frontend (Cursor) |
|------------------|-------------------|
| Lock Render redirects; seed curriculum APIs | `VITE_API_BASE` = Render; callback → home |
| Unit complete + unlock APIs | Course + lesson pages (generate UI fast, polish later) |

**Exit:** Teams login works on production; user finishes Unit 1.

### Week 2 (1–7 Sep) — Assess + Licence
| Backend | Frontend |
|---------|----------|
| Quiz attempt/score + licence issue + verify API | Quiz UI + licence card + public verify page |

**Exit:** Pass quiz → licence → public Valid page.

### Week 3 (8–15 Sep) — Polish + Demo freeze
| Backend | Frontend |
|---------|----------|
| Bugfix, demo seed, logging | UI polish, empty states, E2E with BE |
| **15 Sep: demo freeze (hotfix only)** | **Same** |

**Demo script (15 Sep):** Login → learn → quiz → licence → verify link.

---

## 5. Full spreadsheet Version 1 — time WITH Cursor + Claude

Classic compressed full V1 was ~**6–8 months** (1 BE + 1 FE).

| Scenario | Without AI | **With Cursor + Claude** |
|----------|------------|---------------------------|
| Full V1 (Core + MVP + Foundation + Teams app + admin + consumer + billing + i18n) | ~6–8 months | **~3.5–5 months** |
| Full V1 with 2 BE + 2 FE + AI | ~3.5–4.5 months | **~2–3 months** |
| **15 Sep V1 slice** (Section 3) | Tight / risky | **Achievable** |

### Full V1 phases (AI-assisted calendar, 1+1)

| Phase | Scope | Calendar |
|-------|--------|----------|
| P0 Foundation | Auth, shells, RBAC skeleton | ~1.5–2 weeks |
| P1 Learn + assess | Curriculum, drip, quiz | ~3–4 weeks |
| P2 Teams delivery | Adaptive Cards app, nudges | ~3–4 weeks |
| P3 Licence + verify | Issue, classes, QR, branding | ~2–3 weeks |
| P4 Admin + compliance | Cohorts, bulk, dashboard | ~2.5–3.5 weeks |
| P5 Consumer + EU + billing | Free licence, Article 4, Stripe | ~2.5–3 weeks |
| P6 i18n + analytics + polish | EN/AR/ES, RTL, metrics | ~2–2.5 weeks |
| **Total full V1** | | **~15–21 weeks (~3.5–5 months)** |

---

## 6. Backend vs Frontend ownership

### Backend (Cursor/Claude)
- APIs, models, MongoDB  
- Teams/Microsoft server flows  
- Licence issue/verify, scoring, jobs  
- Render/Azure env  

### Frontend (Cursor/Claude)
- Vue screens, `/auth/callback`, learner UX  
- Verify page, quiz UI, licence card  
- `VITE_API_BASE` per environment  

### Still human-owned
- Curriculum text & quiz questions  
- Azure app permissions / admin consent  
- Final UAT sign-off on 15 Sep  

---

## 7. OUT OF SCOPE for 15 Sep

Full Teams Adaptive Cards bot, Slack, video tracking, nudges, multi-class licences, org branding studio, full RBAC/cohorts/bulk, coverage dashboard, Article 4, HRIS trigger, i18n/RTL, analytics, LinkedIn/social pack, Stripe, SOC 2.

→ **V1.1 / V2 after 15 Sep** (faster with AI than classic, still multi-week).

---

## 8. Leadership one-liner (copy-paste)

> Using **Cursor + Claude**, implementation is roughly **1.7× faster**, but Azure, content, and QA still take real calendar time.  
> **By 15 September 2026** we will ship an AI-accelerated **V1 slice**: Teams login, short AI literacy path, one assessment, digital licence, public verify — not the entire spreadsheet Version 1.  
> **Full spreadsheet V1** with AI assist is about **3.5–5 months** (1+1), not 15 days.

---

## 9. Daily rule while using AI (until 15 Sep)

1. Generate with Cursor/Claude → **human review** before merge  
2. Every day: production login → home must stay green  
3. Do not expand scope beyond Section 3 without cutting something else  
4. Prefer “working demo path” over perfect admin UI  

---

**Bottom line for 15 Sep + Cursor/Claude:**  
**Yes — complete the V1 slice in ~3 weeks is realistic.**  
**No — complete entire spreadsheet Version 1 by 15 Sep is not realistic**, even with AI.
