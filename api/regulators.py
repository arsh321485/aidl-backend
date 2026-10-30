"""The Regulator card: which regulators may oversee the organization's use
of AI and data, from the countries it works in and its industry (asked once,
"About your company"). AIDL works this out — customers never pick or approve
regulators. Admins only: it informs them while they set up the AI policy.

The AIDL team owns this table. Every entry carries the regulator's official
website and a "reviewed" date; an entry is shown to customers only once it
has been through AIDL's compliance review (reviewed = "YYYY-MM-DD"). Until
then it is a draft, visible only where REGULATORS_PREVIEW=True (AIDL's own
test workspaces). Where nothing reviewed exists for a country / industry,
the card says so instead of guessing.

To update: edit an entry, have it reviewed, set its "reviewed" date, bump
TABLE_VERSION.
"""

from __future__ import annotations

from django.conf import settings

TABLE_VERSION = "2026-10-30-draft"
EU = "European Union (any EU country)"
COUNTRIES = ["India", "United Kingdom", "United States", EU, "United Arab Emirates", "Saudi Arabia", "Bahrain",
             "Singapore", "Australia", "Other"]
INDUSTRIES = ["Banking & financial services", "Insurance", "Healthcare", "Government / public sector", "Education",
              "Retail & e-commerce", "Technology / IT services", "Manufacturing", "Hospitality & travel",
              "Professional services (legal, consulting, audit)", "Other"]


def _r(name: str, oversees: str, url: str = "", when: str = "", reviewed: str = "") -> dict:
    return {"name": name, "oversees": oversees, "url": url, "when": when, "reviewed": reviewed}


# Regulators for everyone in a country.
BY_COUNTRY: dict[str, list[dict]] = {
    "India": [
        _r("Data Protection Board of India", "personal data, under the Digital Personal Data Protection Act, 2023",
           "https://www.meity.gov.in"),
        _r("CERT-In (Indian Computer Emergency Response Team)", "cyber security incidents and how fast they must be "
           "reported", "https://www.cert-in.org.in"),
    ],
    "United Kingdom": [
        _r("Information Commissioner's Office (ICO)", "personal data, under the UK GDPR and the Data Protection Act "
           "2018", "https://ico.org.uk"),
    ],
    "United States": [
        _r("Federal Trade Commission (FTC)", "unfair or deceptive practices, including how companies use AI and "
           "consumers' data", "https://www.ftc.gov"),
        _r("California Privacy Protection Agency", "consumers' personal data under the CCPA / CPRA",
           "https://cppa.ca.gov", when="you have customers in California"),
    ],
    EU: [
        _r("Your country's data protection authority", "personal data, under the EU GDPR",
           "https://www.edpb.europa.eu/about-edpb/about-edpb/members_en"),
        _r("Your country's AI Act authority", "the EU AI Act, including Article 4 (AI literacy of your staff)",
           "https://digital-strategy.ec.europa.eu/en/policies/ai-office"),
    ],
    "United Arab Emirates": [
        _r("UAE Data Office", "personal data, under the federal Personal Data Protection Law",
           when="you're outside a financial free zone (DIFC and ADGM have their own data regulators)"),
    ],
    "Saudi Arabia": [
        _r("Saudi Data & AI Authority (SDAIA)", "personal data and AI, under the Personal Data Protection Law",
           "https://sdaia.gov.sa"),
        _r("National Cybersecurity Authority (NCA)", "cyber security controls", "https://nca.gov.sa"),
    ],
    "Bahrain": [
        _r("Personal Data Protection Authority", "personal data, under the Personal Data Protection Law (Law No. 30 "
           "of 2018)"),
    ],
    "Singapore": [
        _r("Personal Data Protection Commission (PDPC)", "personal data, under the PDPA", "https://www.pdpc.gov.sg"),
        _r("Cyber Security Agency of Singapore (CSA)", "cyber security", "https://www.csa.gov.sg"),
    ],
    "Australia": [
        _r("Office of the Australian Information Commissioner (OAIC)", "personal information, under the Privacy Act "
           "1988", "https://www.oaic.gov.au"),
    ],
    "Other": [
        _r("Your national data protection authority", "personal data in your country"),
    ],
}

# Extra regulators for an industry in a country.
BY_INDUSTRY: dict[tuple[str, str], list[dict]] = {
    ("India", "Banking & financial services"): [
        _r("Reserve Bank of India (RBI)", "banks and payment companies, including their IT and outsourcing",
           "https://www.rbi.org.in"),
        _r("Securities and Exchange Board of India (SEBI)", "securities markets", "https://www.sebi.gov.in",
           when="you're a listed company or a market intermediary"),
    ],
    ("India", "Insurance"): [
        _r("Insurance Regulatory and Development Authority of India (IRDAI)", "insurers and their IT and data",
           "https://irdai.gov.in"),
    ],
    ("United Kingdom", "Banking & financial services"): [
        _r("Financial Conduct Authority (FCA)", "financial firms' conduct, including their use of AI",
           "https://www.fca.org.uk"),
        _r("Prudential Regulation Authority (PRA)", "banks', insurers' and investment firms' safety",
           "https://www.bankofengland.co.uk/prudential-regulation", when="you're a bank, insurer or major investment firm"),
    ],
    ("United Kingdom", "Insurance"): [
        _r("Financial Conduct Authority (FCA)", "insurers' conduct", "https://www.fca.org.uk"),
    ],
    ("United States", "Healthcare"): [
        _r("HHS Office for Civil Rights", "patients' health information, under HIPAA", "https://www.hhs.gov/ocr"),
    ],
    ("United States", "Banking & financial services"): [
        _r("Your financial regulators (e.g. SEC, FINRA, CFPB, banking regulators)", "financial firms, including "
           "their use of data and AI"),
    ],
    ("United Arab Emirates", "Banking & financial services"): [
        _r("Central Bank of the UAE", "banks and financial institutions", "https://www.centralbank.ae"),
    ],
    ("Saudi Arabia", "Banking & financial services"): [
        _r("Saudi Central Bank (SAMA)", "banks, insurers and payment companies", "https://www.sama.gov.sa"),
    ],
    ("Bahrain", "Banking & financial services"): [
        _r("Central Bank of Bahrain (CBB)", "banks, insurers and financial institutions", "https://www.cbb.gov.bh"),
    ],
    ("Singapore", "Banking & financial services"): [
        _r("Monetary Authority of Singapore (MAS)", "financial institutions, including technology risk",
           "https://www.mas.gov.sg"),
    ],
    ("Australia", "Banking & financial services"): [
        _r("Australian Prudential Regulation Authority (APRA)", "banks, insurers and super funds",
           "https://www.apra.gov.au"),
        _r("Australian Securities and Investments Commission (ASIC)", "financial services and markets",
           "https://asic.gov.au"),
    ],
}


def _visible(r: dict) -> bool:
    return bool(r["reviewed"]) or getattr(settings, "REGULATORS_PREVIEW", False)


def regulators_for(countries: list[str], industry: str) -> list[dict]:
    """Reviewed regulators (and drafts where previews are on), no repeats."""
    out, seen = [], set()
    for c in countries:
        for r in BY_COUNTRY.get(c, []) + BY_INDUSTRY.get((c, industry), []):
            if _visible(r) and r["name"] not in seen:
                seen.add(r["name"])
                out.append(r)
    return out


def card_blocks(org_name: str, countries: list[str], industry: str) -> list:
    where = ", ".join(c.replace(" (any EU country)", "") for c in countries) or "—"
    regs = regulators_for(countries, industry)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🏛 Regulators for {org_name}"[:150], "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{where} · {industry or '—'}"}]},
    ]
    if not regs:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
            "AIDL's compliance team is preparing the regulator details for your countries and industry. They'll "
            "appear here once they've been reviewed — nothing to do on your side."}})
    for r in regs:
        name = f"<{r['url']}|{r['name']}>" if r["url"] else r["name"]
        text = f"*{name}*\nOversees {r['oversees']}."
        if r["when"]:
            text += f"\n_May apply if {r['when']}._"
        if not r["reviewed"]:
            text += "\n`DRAFT · not yet reviewed`"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}})
    blocks += [
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "Regulators that *may* oversee how you use AI and data, from your countries and industry. Prepared and "
            "kept up to date by AIDL — not legal advice. Only admins see this."}]},
        {"type": "actions", "block_id": "aidl_regulators", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✏️ Change country or industry", "emoji": True},
             "action_id": "aidl_company_open", "value": "change"}]},
    ]
    return blocks
