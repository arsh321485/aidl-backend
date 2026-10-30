"""The laws card: which laws and rules apply to the organization, from the
countries it works in and its industry (asked once, "About your company").

Only admins see it. It's a starting list, not legal advice — the wording
says so, and it should be reviewed by a lawyer before it's relied on.
"""

from __future__ import annotations

EU = "European Union (any EU country)"
COUNTRIES = ["India", "United Kingdom", "United States", EU, "United Arab Emirates", "Saudi Arabia", "Bahrain",
             "Singapore", "Australia", "Other"]
INDUSTRIES = ["Banking & financial services", "Insurance", "Healthcare", "Government / public sector", "Education",
              "Retail & e-commerce", "Technology / IT services", "Manufacturing", "Hospitality & travel",
              "Professional services (legal, consulting, audit)", "Other"]

BY_COUNTRY = {
    "India": ["Digital Personal Data Protection Act, 2023 (DPDP Act)",
              "Information Technology Act, 2000 and CERT-In directions (report cyber incidents within 6 hours)"],
    "United Kingdom": ["UK GDPR and the Data Protection Act 2018"],
    "United States": ["State privacy laws where you have customers (e.g. California CCPA / CPRA)"],
    EU: ["EU GDPR",
         "EU AI Act, Article 4 — take measures to build your staff's AI literacy (applies since 2 Feb 2025)"],
    "United Arab Emirates": ["UAE Personal Data Protection Law (Federal Decree-Law No. 45 of 2021)"],
    "Saudi Arabia": ["Saudi Personal Data Protection Law (PDPL)"],
    "Bahrain": ["Bahrain Personal Data Protection Law (Law No. 30 of 2018)"],
    "Singapore": ["Personal Data Protection Act 2012 (PDPA)"],
    "Australia": ["Privacy Act 1988"],
    "Other": ["Your national data protection law"],
}
BY_INDUSTRY = {
    "Banking & financial services": ["Your central bank / financial regulator's IT and outsourcing rules",
                                     "PCI DSS, if you take card payments"],
    "Insurance": ["Your insurance regulator's IT and data rules"],
    "Healthcare": ["Health-data rules in your country (e.g. HIPAA in the US)"],
    "Government / public sector": ["Your government's information security standards"],
    "Retail & e-commerce": ["PCI DSS, if you take card payments", "Consumer protection rules for online sales"],
    "Technology / IT services": ["ISO/IEC 27001 or SOC 2 — not laws, but customers often ask for them"],
}


def laws_for(countries: list[str], industry: str) -> list[str]:
    out: list[str] = []
    for c in countries:
        for law in BY_COUNTRY.get(c, []):
            if law not in out:
                out.append(law)
    for law in BY_INDUSTRY.get(industry, []):
        if law not in out:
            out.append(law)
    if EU not in countries:
        out.append("EU AI Act, Article 4 (AI literacy) — only if you have staff or customers in the EU")
    return out


def card_blocks(org_name: str, countries: list[str], industry: str) -> list:
    where = ", ".join(c.replace(" (any EU country)", "") for c in countries) or "—"
    items = "\n".join(f"›  {law}" for law in laws_for(countries, industry))
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"⚖️ Laws and rules for {org_name}"[:150],
                                    "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{where} · {industry or '—'}"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": items[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "From your countries and industry. A starting "
                                          "list, not legal advice — check it with your lawyer. Only admins see this."}]},
        {"type": "actions", "block_id": "aidl_laws", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✏️ Change country or industry", "emoji": True},
             "action_id": "aidl_company_open", "value": "change"}]},
    ]
