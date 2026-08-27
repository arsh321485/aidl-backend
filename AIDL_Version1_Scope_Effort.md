# AIDL — Version 1 Plan Against Target Date: **15 September 2026**

**Source:** [AIDL Feature List — Version Roadmap](https://docs.google.com/spreadsheets/d/1qXLcQqnFqlg3UxRZt_aqNMNlSzOi481SUuJDWOw4uh8/edit?gid=0#gid=0)  
**Today (planning baseline):** 25 August 2026  
**Target go-live / demo date:** **15 September 2026**  
**Working days available:** ~**15 working days** (~21 calendar days)  
**Team assumption:** 1 Backend + 1 Frontend (parallel)

---

## Reality check

Full spreadsheet **Version 1** (Core + MVP + Foundation + consumer + Teams app + billing + i18n) needs **~6–8 months** with 1+1 engineers.

**It cannot all ship by 15 Sep.**

By **15 Sep** we ship a **Version 1 Slice (V1-Sep15)** — the smallest end-to-end product that proves AIDL:

> **Microsoft Teams login → short learning → quiz → digital licence → public verify**

Everything else from the sheet moves to **V1.1 (after 15 Sep)** or **V2**.

---

## 1. What ships by 15 September (IN SCOPE)

| # | Spreadsheet feature | Module | Backend work | Frontend work | BE days | FE days |
|---|---------------------|--------|--------------|---------------|---------|---------|
| 1 | Microsoft Teams auth (harden + prod) | M2 / Platform | Prod env, callback, `/me`, refresh, launch URL | `/auth/callback`, session, home after login, `VITE_API_BASE` = Render | 2 | 2 |
| 2 | Core AI literacy curriculum (**1 short track**, 3–5 units) | M4 Foundation | Curriculum/unit APIs; seed content | Learner course list + lesson pages | 3 | 4 |
| 3 | Microlearning drip (**simple sequential unlock**, not full scheduler) | M2 MVP | Unlock next unit on complete | Progress bar / next unit CTA | 2 | 2 |
| 4 | Question bank + randomisation (**small bank**, 1 assessment) | M3 MVP | Questions CRUD/seed; start/submit attempt; score | Quiz UI; result screen | 3 | 3 |
| 5 | Configurable pass threshold (**one global %**, e.g. 70%) | M3 | Threshold config + gate | Pass/fail messaging | 1 | 0.5 |
| 6 | Licence issuance engine (**one class**: e.g. Learner / Full) | M1 Core | Licence model + issue after pass | Licence success card UI | 3 | 3 |
| 7 | Verifiable public URL + QR | M1 Core | Public verify API + slug | Public verify page + QR | 2 | 2 |
| 8 | Free licence generation (**consumer path reuse** same licence) | M11 MVP | Allow enroll_as individual flow | Simple register/start CTA if needed | 1 | 1 |
| 9 | Deploy + smoke QA | Platform | Render health, CORS, redirects | Vercel env, E2E smoke | 1 | 1 |
| | **TOTAL (ideal)** | | | | **~18d** | **~18.5d** |

With **15 working days** and **1+1 parallel**, capacity ≈ **15 BE + 15 FE days**.

→ Plan is **tight**: must stay on this list only; any slip cuts content depth, not the flow.

**Calendar:** 25 Aug → 15 Sep = **3 weeks** → fits **only** if no major Azure/content blockers.

---

## 2. Week-by-week plan (25 Aug → 15 Sep)

### Week 1 — 25 Aug to 31 Aug (Foundation + Learn)

| Day focus | Backend | Frontend |
|-----------|---------|----------|
| Mon–Tue | Harden Teams auth on Render; confirm `MS_REDIRECT_URI` + `AUTH_SUCCESS_REDIRECT` | `VITE_API_BASE` → Render; finish `/auth/callback` → `/home` |
| Wed–Thu | Curriculum models + APIs; seed 3–5 units | Home + course outline + lesson page |
| Fri | Unit complete API; sequential unlock | Progress UI; next-unit flow |

**Exit criteria:** User logs in with Teams and completes Unit 1 in the web app.

---

### Week 2 — 1 Sep to 7 Sep (Assess + Licence)

| Day focus | Backend | Frontend |
|-----------|---------|----------|
| Mon–Tue | Question bank + attempt/score APIs | Quiz UI |
| Wed | Pass threshold + retake once (simple) | Pass/fail + retry |
| Thu–Fri | Licence issue API + public verify endpoint | Licence card + verify page + QR |

**Exit criteria:** Pass quiz → licence created → public URL shows Valid.

---

### Week 3 — 8 Sep to 15 Sep (Polish + Demo)

| Day focus | Backend | Frontend |
|-----------|---------|----------|
| Mon–Tue | Bugfix; seed demo org/user; logging | UI polish; empty/error states |
| Wed–Thu | Staging E2E; Azure/Teams edge cases | E2E with FE engineer; share assets optional |
| **Fri 15 Sep** | **Demo freeze** — hotfix only | **Demo freeze** |

**Exit criteria (15 Sep demo script):**

1. Click Teams → Microsoft login  
2. Land on AIDL home (not blank callback)  
3. Complete short curriculum  
4. Pass assessment  
5. See digital licence  
6. Open public verify link / QR → Valid  

---

## 3. Explicitly OUT OF SCOPE for 15 Sep (do after)

| Spreadsheet item | When |
|------------------|------|
| Full Microsoft Teams **app** (Adaptive Cards in Teams) | V1.1 |
| Slack app | V1.1 / V2 |
| Video watch-through tracking | V1.1 |
| Escalating nudges / auto cards | V1.1 |
| Licence classes (all 4 visuals) | V1.1 (ship **1 class** on 15 Sep) |
| Configurable validity per cohort / version stamping / org branding | V1.1 |
| Renewal workflow, live sessions | V2 |
| Scenario-based items (marked v2) | V2 |
| Full RBAC (6 roles), cohorts, bulk CSV, policy-as-config | V1.1 |
| Coverage dashboard (full) | V1.1 (optional simple count later) |
| Article 4 EU check | V1.1 |
| New-joiner HRIS trigger, campaign scheduler | V2 |
| Multi-language EN/AR/ES + RTL | V1.1 |
| Completion analytics / departmental | V1.1 / V2 |
| Social share, LinkedIn object, ask employer | V1.1 |
| Self-serve Stripe checkout | V1.1 |
| SOC 2 | Parallel process track (not eng demo) |

---

## 4. Backend checklist (15 Sep)

- [ ] Production Teams login/callback/refresh/me/launch stable on Render  
- [ ] `AUTH_SUCCESS_REDIRECT` → `https://aidl-frontend-8owk.vercel.app/auth/callback`  
- [ ] Curriculum + units CRUD/seed APIs  
- [ ] Mark unit complete + sequential unlock  
- [ ] Assessment start/submit/score + pass threshold  
- [ ] Licence create after pass  
- [ ] Public licence verify by slug/token  
- [ ] CORS + env for Vercel frontend  
- [ ] Demo data seeded  

**Backend does NOT build by 15 Sep:** Teams Adaptive Card bot, Slack, billing, RBAC matrix, bulk import, i18n APIs, analytics warehouse.

---

## 5. Frontend checklist (15 Sep)

- [ ] `VITE_API_BASE=https://aidl-backend.onrender.com` on Vercel  
- [ ] `/auth/callback` saves tokens, opens Teams if needed, goes to `/home`  
- [ ] Home (authenticated)  
- [ ] Course + lesson pages  
- [ ] Quiz + results  
- [ ] Licence card page  
- [ ] Public `/verify/:id` (or similar) + QR  
- [ ] Basic error/loading states  

**Frontend does NOT build by 15 Sep:** Admin CMS full, branding studio, dashboards, i18n/RTL, checkout, LinkedIn share pack.

---

## 6. Capacity math (15 Sep)

| | Available | Planned load | Status |
|--|-----------|--------------|--------|
| Backend | ~15 working days | ~18 ideal days | **Over by ~3d** → cut content (3 units not 5) or drop QR if needed |
| Frontend | ~15 working days | ~18.5 ideal days | **Over by ~3.5d** → simplify lesson UI; QR can be link-only |

**Must-protect path:** Login → Learn → Quiz → Licence → Verify  
**First cuts if behind (in order):**

1. QR image (keep plain verify URL only)  
2. Reduce units from 5 → 3  
3. Skip consumer “free register” (Teams org login only)  
4. Skip “open Teams platform” auto-open (login to AIDL web only)

---

## 7. Daily stand-up questions (until 15 Sep)

1. Is Teams login → home working on **production** URLs (not localhost)?  
2. Can a user finish **one lesson** today?  
3. Blockers on Azure / Render / Vercel / content?  
4. Are we still inside the **IN SCOPE** table (Section 1)?

---

## 8. Message for leadership (copy-paste)

> Target **15 September 2026** is **21 calendar days / ~15 working days**.  
> Full spreadsheet Version 1 is a **multi-month** programme.  
> For 15 Sep we will deliver an **end-to-end V1 slice**: Microsoft Teams sign-in, short AI literacy path, one assessment, digital licence issuance, and public verification.  
> Full Teams in-app Adaptive Cards, Slack, billing, multi-role admin, i18n/RTL, and advanced compliance features are scheduled **after 15 Sep (V1.1 / V2)**.

---

## 9. After 15 Sep — V1.1 backlog (next 4–6 weeks)

Priority order suggested:

1. Org branding + licence classes visuals  
2. Basic admin RBAC + coverage counts  
3. Teams Adaptive Cards delivery  
4. Nudges + expiry reminder  
5. Article 4 + consumer share / LinkedIn  
6. EN + AR (RTL)  
7. Checkout (Stripe)

---

**File:** `AIDL_Version1_Scope_Effort.md` (this plan replaces the long full-V1 timeline for the **15 Sep** decision).  
Full-sheet feature inventory remains useful for roadmap; **execution until 15 Sep follows Sections 1–6 only.**
