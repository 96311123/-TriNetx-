#!/usr/bin/env python3
"""
Weekly PubMed harvester: Tzu Chi (慈濟體系) publications using the TriNetX database.

Pipeline
  1. ESearch (history server)  -> list of PMIDs
  2. EFetch XML in batches      -> parse article / author / affiliation records
  3. Classify every affiliation string into a Tzu Chi campus (院區) / 慈濟大學
  4. Write docs/data/{articles.json, summary.json, articles.csv,
     author_affiliations.csv, tzuchi_trinetx.xlsx}

Run:  python scripts/fetch_pubmed.py            (fetch from PubMed and build)
      python scripts/fetch_pubmed.py --from-json docs/data/articles.json
                                                 (rebuild outputs only, no network)
      python scripts/fetch_pubmed.py --no-external  (PubMed only; apply cached enrichment)
Env:  NCBI_API_KEY (optional), NCBI_EMAIL / CONTACT_EMAIL (polite pools for NCBI,
      OpenAlex, Crossref), OPENALEX_API_KEY (optional)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich import apply_cache, load_cache, refresh_cache, save_cache  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "docs" / "data"

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL = "tzuchi-trinetx-tracker"

QUERY = (
    '(Tzu Chi[ad] OR "Buddhist Tzu Chi"[ad] OR "Tzu-Chi"[ad] OR "Dalin Tzu Chi"[ad] '
    'OR "Taipei Tzu Chi"[ad] OR "Hualien Tzu Chi"[ad] OR "Tzu Chi University"[ad] '
    'OR "Tzu Chi Medical Foundation"[ad]) '
    'AND (TriNetX[tiab] OR "TriNetX"[All Fields] OR "TriNetX Research Network"[tiab])'
)

# ---------------------------------------------------------------------------
# Campus classification
# Order matters: the first matching rule wins for a given affiliation string,
# but one author with several affiliations can map to several campuses.
# ---------------------------------------------------------------------------
TC = r"tzu[\s-]*chi"  # "Tzu Chi" / "Tzu-Chi" / "TzuChi"

# City tokens for each hospital campus.
CITY_TOKENS: list[tuple[str, str]] = [
    ("花蓮慈濟醫院", r"hualien|hua-?lien"),
    ("大林慈濟醫院", r"dalin|da-?lin|chiayi|chia-?yi"),
    ("台北慈濟醫院", r"taipei|xindian|hsin-?tien|sindian"),
    ("台中慈濟醫院", r"taichung|tai-?chung"),
    ("玉里慈濟醫院", r"yuli|yu-?li"),
    ("關山慈濟醫院", r"guanshan|kuanshan|kuan-?shan|guan-?shan"),
    ("斗六慈濟醫院", r"douliu|touliu|dou-?liu|tou-?liu|yunlin|yun-?lin"),
]
# Explicit "<City> Tzu Chi Hospital" forms — strongest evidence.
_EXPLICIT = [(lbl, re.compile(rf"(?:{city})\s+{TC}", re.I)) for lbl, city in CITY_TOKENS]
_CITY = [(lbl, re.compile(city, re.I)) for lbl, city in CITY_TOKENS]

_TC_ANY = re.compile(TC, re.I)
_TCUST = re.compile(rf"{TC}\s+university\s+of\s+science|{TC}\s+college\s+of\s+(technology|nursing)|{TC}\s+(university\s+of\s+)?science\s+and\s+technology", re.I)
_TCU = re.compile(rf"{TC}\s+university(?!\s+of\s+science)|{TC}\s+(medical\s+)?college(?!\s+of\s+(technology|nursing))|{TC}\s+school\s+of\s+medicine", re.I)
_HOSP = re.compile(rf"{TC}\s+(general\s+)?(hospital|medical\s+cent(er|re))|{TC}\s+medical\s+foundation|buddhist\s+{TC}|{TC}\s+(healthcare|medical)\s+(system|mission)", re.I)

CAMPUS_ORDER = [lbl for lbl, _ in CITY_TOKENS] + ["慈濟大學", "慈濟科技大學", "慈濟醫療財團法人(未指明院區)"]
HOSPITAL_CAMPUSES = [lbl for lbl, _ in CITY_TOKENS]
UNSPECIFIED = "慈濟醫療財團法人(未指明院區)"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

CORR_SOURCE_LABEL = {
    "pmc": "PMC全文",
    "openalex": "OpenAlex",
    "pubmed_email": "PubMed e-mail",
    "last_author": "末位作者(推定)",
    "none": "",
}


def classify_affiliation(aff: str) -> list[str]:
    """Return every Tzu Chi campus / school label found in one affiliation string.

    Strategy
      * 慈濟大學 / 慈濟科技大學 are detected by name.
      * A hospital mention is detected by "Tzu Chi Hospital / Medical Center /
        Medical Foundation / Buddhist Tzu Chi". Its campus is decided first by
        the explicit "<City> Tzu Chi ..." form; if absent, by any city token
        in the string; if still absent -> 未指明院區.
      * One string can therefore yield e.g. ["大林慈濟醫院", "慈濟大學"].
    """
    text = aff or ""
    if not _TC_ANY.search(text):
        return []
    hits: list[str] = []

    is_tcust = bool(_TCUST.search(text))
    is_tcu = bool(_TCU.search(text))
    is_hosp = bool(_HOSP.search(text))

    if is_hosp:
        campus = [lbl for lbl, rx in _EXPLICIT if rx.search(text)]
        if not campus:
            campus = [lbl for lbl, rx in _CITY if rx.search(text)]
            # "New Taipei" appears in Taipei Tzu Chi addresses; a lone "Taipei"
            # may also be a non-Tzu-Chi co-affiliation, so only keep the city
            # fallback when exactly one campus city is present.
            if len(campus) > 1:
                campus = []
        hits += campus or [UNSPECIFIED]
    if is_tcu:
        hits.append("慈濟大學")
    if is_tcust:
        hits.append("慈濟科技大學")
    if not hits:
        hits.append(UNSPECIFIED)
    return list(OrderedDict.fromkeys(hits))


# ---------------------------------------------------------------------------
# E-utilities
# ---------------------------------------------------------------------------
def _params(**kw):
    p = {"tool": TOOL}
    if os.getenv("NCBI_EMAIL"):
        p["email"] = os.environ["NCBI_EMAIL"]
    if os.getenv("NCBI_API_KEY"):
        p["api_key"] = os.environ["NCBI_API_KEY"]
    p.update(kw)
    return p


def _get(url, **params):
    for attempt in range(5):
        try:
            r = requests.get(url, params=_params(**params), timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            wait = 2 ** attempt
            print(f"  retry {attempt+1}: {e} (sleep {wait}s)", file=sys.stderr)
            time.sleep(wait)
    raise SystemExit("E-utilities request failed repeatedly")


def esearch_all(query: str) -> tuple[list[str], dict]:
    r = _get(f"{EUTILS}/esearch.fcgi", db="pubmed", term=query, usehistory="y",
             retmax=10000, retmode="json")
    res = r.json()["esearchresult"]
    ids = res["idlist"]
    meta = {"count": int(res["count"]), "querytranslation": res.get("querytranslation", "")}
    return ids, meta


def efetch_xml(pmids: list[str], batch=100) -> list[ET.Element]:
    arts = []
    for i in range(0, len(pmids), batch):
        chunk = pmids[i:i + batch]
        r = _get(f"{EUTILS}/efetch.fcgi", db="pubmed", id=",".join(chunk), retmode="xml")
        root = ET.fromstring(r.content)
        arts.extend(root.findall("PubmedArticle"))
        time.sleep(0.34 if not os.getenv("NCBI_API_KEY") else 0.11)
        print(f"  fetched {min(i+batch, len(pmids))}/{len(pmids)}", file=sys.stderr)
    return arts


# ---------------------------------------------------------------------------
# XML -> record
# ---------------------------------------------------------------------------
def _text(el, path, default=""):
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else default


def _itertext(el, path):
    x = el.find(path)
    return "".join(x.itertext()).strip() if x is not None else ""


def parse_article(pa: ET.Element) -> dict:
    mc = pa.find("MedlineCitation")
    art = mc.find("Article")
    pmid = _text(mc, "PMID")

    journal = art.find("Journal")
    year = _text(journal, "JournalIssue/PubDate/Year")
    if not year:
        md = _text(journal, "JournalIssue/PubDate/MedlineDate")
        m = re.search(r"\d{4}", md)
        year = m.group(0) if m else ""
    month = _text(journal, "JournalIssue/PubDate/Month")

    doi = ""
    for e in art.findall("ELocationID"):
        if e.get("EIdType") == "doi":
            doi = (e.text or "").strip()
    pmc = ""
    for a in pa.findall("PubmedData/ArticleIdList/ArticleId"):
        if a.get("IdType") == "doi" and not doi:
            doi = (a.text or "").strip()
        if a.get("IdType") == "pmc":
            pmc = (a.text or "").strip()

    entrez_date = ""
    for d in pa.findall("PubmedData/History/PubMedPubDate"):
        if d.get("PubStatus") in ("pubmed", "entrez"):
            entrez_date = f"{_text(d,'Year')}-{_text(d,'Month').zfill(2)}-{_text(d,'Day').zfill(2)}"
            if d.get("PubStatus") == "pubmed":
                break

    authors = []
    for au in art.findall("AuthorList/Author"):
        if au.get("ValidYN", "Y") == "N":
            continue
        last = _text(au, "LastName")
        fore = _text(au, "ForeName")
        coll = _text(au, "CollectiveName")
        name = coll or (f"{fore} {last}".strip())
        affs = [(_itertext(ai, "Affiliation")) for ai in au.findall("AffiliationInfo")]
        affs = [a for a in affs if a]
        orcid = ""
        for ident in au.findall("Identifier"):
            if (ident.get("Source") or "").upper() == "ORCID" and ident.text:
                m = re.search(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", ident.text)
                orcid = m.group(0) if m else ident.text.strip()
        authors.append({"name": name, "last": last, "fore": fore,
                        "initials": _text(au, "Initials"), "affiliations": affs,
                        "orcid_pubmed": orcid})

    pub_types = [(pt.text or "").strip() for pt in art.findall("PublicationTypeList/PublicationType")]

    return {
        "pmid": pmid,
        "title": _itertext(art, "ArticleTitle"),
        "journal": _text(journal, "Title"),
        "journal_abbr": _text(journal, "ISOAbbreviation"),
        "year": year,
        "month": month,
        "volume": _text(journal, "JournalIssue/Volume"),
        "issue": _text(journal, "JournalIssue/Issue"),
        "pages": _text(art, "Pagination/MedlinePgn"),
        "doi": doi,
        "pmc": pmc,
        "entrez_date": entrez_date,
        "pub_types": pub_types,
        "authors": authors,
    }


# ---------------------------------------------------------------------------
# Enrichment: campus tagging, first / corresponding author
# ---------------------------------------------------------------------------
def enrich(rec: dict) -> dict:
    authors = rec["authors"]
    article_campuses: list[str] = []
    for idx, au in enumerate(authors):
        campuses = []
        emails = []
        for aff in au["affiliations"]:
            campuses += classify_affiliation(aff)
            emails += EMAIL_RE.findall(aff)
        au["campuses"] = list(OrderedDict.fromkeys(campuses))
        au["emails"] = list(OrderedDict.fromkeys(emails))
        au["position"] = idx + 1
        au["is_first"] = idx == 0
        au["is_last"] = idx == len(authors) - 1
        article_campuses += au["campuses"]

    # Corresponding author — precedence:
    #   1) PMC full-text JATS markup        (source "pmc")
    #   2) OpenAlex authorships.is_corresponding (source "openalex")
    #   3) PubMed affiliation contains e-mail   (source "pubmed_email")
    #   4) last author                           (source "last_author")
    ext = rec.get("ext") or {}
    corr, method = [], "none"
    for key, label in (("corr_pmc", "pmc"), ("corr_openalex", "openalex")):
        hits = [a for a in authors if a.get(key)]
        if hits:
            corr, method = hits, label
            break
    if not corr:
        with_email = [a for a in authors if a["emails"]]
        if with_email:
            corr, method = with_email, "pubmed_email"
        elif authors:
            corr, method = authors[-1:], "last_author"
    for a in authors:
        a["is_corresponding"] = a in corr
        a.setdefault("orcid", a.get("orcid_pubmed", ""))
    rec["corresponding_method"] = method
    rec["corresponding_source"] = CORR_SOURCE_LABEL.get(method, method)
    # agreement check when both full text and OpenAlex are available
    pmc_set = {a["name"] for a in authors if a.get("corr_pmc")}
    oa_set = {a["name"] for a in authors if a.get("corr_openalex")}
    rec["corresponding_agreement"] = (
        "" if not (pmc_set and oa_set) else ("一致" if pmc_set == oa_set else "不一致")
    )
    rec["publisher"] = ext.get("publisher", "")
    rec["citations"] = ext.get("citations") if ext.get("citations") is not None else ext.get("openalex_citations")
    rec["pmc"] = rec.get("pmc") or ext.get("pmcid_used", "")

    rec["campuses"] = list(OrderedDict.fromkeys(article_campuses))
    rec["first_author"] = authors[0]["name"] if authors else ""
    rec["first_author_campuses"] = authors[0]["campuses"] if authors else []
    rec["corresponding_authors"] = [a["name"] for a in corr]
    rec["corresponding_campuses"] = list(OrderedDict.fromkeys(c for a in corr for c in a["campuses"]))
    rec["n_authors"] = len(authors)
    rec["n_tzuchi_authors"] = sum(1 for a in authors if a["campuses"])
    rec["pubmed_url"] = f"https://pubmed.ncbi.nlm.nih.gov/{rec['pmid']}/"
    return rec


def summarize(records: list[dict], meta: dict) -> dict:
    by_campus = Counter()
    first_by_campus = Counter()
    corr_by_campus = Counter()
    authors_by_campus = Counter()
    by_year = Counter()
    by_journal = Counter()
    by_corr_source = Counter()
    for r in records:
        by_corr_source[r.get("corresponding_source") or "無"] += 1
        by_year[r["year"] or "未知"] += 1
        by_journal[r["journal"]] += 1
        for c in r["campuses"]:
            by_campus[c] += 1
        for c in r["first_author_campuses"]:
            first_by_campus[c] += 1
        for c in r["corresponding_campuses"]:
            corr_by_campus[c] += 1
        for a in r["authors"]:
            for c in a["campuses"]:
                authors_by_campus[c] += 1

    campus_table = []
    for c in CAMPUS_ORDER:
        if by_campus[c] or first_by_campus[c] or corr_by_campus[c]:
            campus_table.append({
                "campus": c,
                "articles": by_campus[c],
                "first_author_articles": first_by_campus[c],
                "corresponding_author_articles": corr_by_campus[c],
                "author_slots": authors_by_campus[c],
            })

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "query": QUERY,
        "query_translation": meta.get("querytranslation", ""),
        "pubmed_count": meta.get("count", len(records)),
        "n_articles": len(records),
        "n_tzu_chi_university": by_campus["慈濟大學"],
        "campus_table": campus_table,
        "by_year": [{"year": y, "articles": n} for y, n in sorted(by_year.items())],
        "top_journals": [{"journal": j, "articles": n} for j, n in by_journal.most_common(15)],
        "note_counting": "一篇文章若有多個院區的作者，會在每個院區各計一次；第一作者/通訊作者若有多重隸屬亦各計一次。",
        "corresponding_sources": [{"source": k, "articles": by_corr_source[k]}
                                  for k in list(CORR_SOURCE_LABEL.values()) + ["無"] if k and by_corr_source[k]],
        "corresponding_disagreements": sum(1 for r in records if r.get("corresponding_agreement") == "不一致"),
        "note_corresponding": "通訊作者判定優先順序：① PMC 全文 JATS 標記 → ② OpenAlex is_corresponding → ③ PubMed 隸屬含 e-mail → ④ 末位作者。每篇文章標示採用的來源。",
    }


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
def write_csvs(records: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "articles.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["PMID", "年份", "標題", "期刊", "期刊縮寫", "卷", "期", "頁碼", "DOI", "PMC",
                    "作者(全部)", "第一作者", "第一作者院區", "通訊作者", "通訊作者院區", "通訊作者來源",
                    "PMC/OpenAlex一致性", "文章涉及院區", "慈濟作者數", "作者總數", "出版社", "被引次數",
                    "PubMed收錄日", "PubMed連結"])
        for r in records:
            w.writerow([r["pmid"], r["year"], r["title"], r["journal"], r["journal_abbr"],
                        r["volume"], r["issue"], r["pages"], r["doi"], r["pmc"],
                        "; ".join(a["name"] for a in r["authors"]),
                        r["first_author"], "; ".join(r["first_author_campuses"]),
                        "; ".join(r["corresponding_authors"]), "; ".join(r["corresponding_campuses"]),
                        r.get("corresponding_source", ""), r.get("corresponding_agreement", ""),
                        "; ".join(r["campuses"]), r["n_tzuchi_authors"], r["n_authors"],
                        r.get("publisher", ""), "" if r.get("citations") is None else r["citations"],
                        r["entrez_date"], r["pubmed_url"]])
    with open(DATA_DIR / "author_affiliations.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["PMID", "年份", "作者順位", "作者", "ORCID", "第一作者", "通訊作者", "通訊作者來源",
                    "PMC全文標記", "OpenAlex標記", "院區分類", "隸屬機構(原文)"])
        for r in records:
            for a in r["authors"]:
                affs = a["affiliations"] or [""]
                for aff in affs:
                    w.writerow([r["pmid"], r["year"], a["position"], a["name"], a.get("orcid", ""),
                                "Y" if a["is_first"] else "", "Y" if a["is_corresponding"] else "",
                                r.get("corresponding_source", "") if a["is_corresponding"] else "",
                                "Y" if a.get("corr_pmc") else "", "Y" if a.get("corr_openalex") else "",
                                "; ".join(classify_affiliation(aff)), aff])


def write_xlsx(records: list[dict], summary: dict):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="1F4E78")
    hdr_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    campus_fill = {
        "花蓮慈濟醫院": "FCE4D6", "大林慈濟醫院": "E2EFDA", "台北慈濟醫院": "DDEBF7",
        "台中慈濟醫院": "FFF2CC", "玉里慈濟醫院": "EDEDED", "關山慈濟醫院": "EDEDED",
        "斗六慈濟醫院": "EDEDED", "慈濟大學": "E4DFEC", "慈濟科技大學": "E4DFEC",
        "慈濟醫療財團法人(未指明院區)": "F2F2F2",
    }

    def style_header(ws, ncol, widths=None):
        for c in range(1, ncol + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill, cell.font, cell.border = hdr_fill, hdr_font, border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        if widths:
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w

    # --- Sheet 1: 總覽 ---
    ws = wb.active
    ws.title = "總覽"
    ws["A1"] = "慈濟體系 TriNetX 發表文章追蹤"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"資料更新時間 (UTC): {summary['generated_at']}"
    ws["A3"] = f"PubMed 命中篇數: {summary['pubmed_count']}  |  已解析篇數: {summary['n_articles']}"
    ws["A4"] = "搜尋策略:"; ws["B4"] = summary["query"]; ws["B4"].alignment = Alignment(wrap_text=True)
    ws["A5"] = "計數規則:"; ws["B5"] = summary["note_counting"]
    ws["A6"] = "通訊作者:"; ws["B6"] = summary["note_corresponding"]
    ws.column_dimensions["A"].width = 30; ws.column_dimensions["B"].width = 110
    row = 8
    headers = ["院區 / 單位", "文章數(任一作者)", "第一作者文章數", "通訊作者文章數", "作者人次"]
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=i, value=h)
        c.fill, c.font, c.border = hdr_fill, hdr_font, border
    for ct in summary["campus_table"]:
        row += 1
        vals = [ct["campus"], ct["articles"], ct["first_author_articles"],
                ct["corresponding_author_articles"], ct["author_slots"]]
        for i, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=i, value=v); c.border = border
            c.fill = PatternFill("solid", fgColor=campus_fill.get(ct["campus"], "FFFFFF"))
    for i, w in enumerate([30, 18, 18, 22, 12], 1):
        ws.column_dimensions[get_column_letter(i)].width = max(ws.column_dimensions[get_column_letter(i)].width or 0, w)
    row += 2
    ws.cell(row=row, column=1, value="年份").fill = hdr_fill
    ws.cell(row=row, column=1).font = hdr_font
    ws.cell(row=row, column=2, value="文章數").fill = hdr_fill
    ws.cell(row=row, column=2).font = hdr_font
    for y in summary["by_year"]:
        row += 1
        ws.cell(row=row, column=1, value=y["year"]).border = border
        ws.cell(row=row, column=2, value=y["articles"]).border = border
    row += 2
    for i, h in enumerate(["通訊作者判定來源", "文章數"], 1):
        c = ws.cell(row=row, column=i, value=h); c.fill, c.font, c.border = hdr_fill, hdr_font, border
    for cs in summary.get("corresponding_sources", []):
        row += 1
        ws.cell(row=row, column=1, value=cs["source"]).border = border
        ws.cell(row=row, column=2, value=cs["articles"]).border = border
    row += 1
    ws.cell(row=row, column=1, value="PMC 與 OpenAlex 不一致篇數").border = border
    ws.cell(row=row, column=2, value=summary.get("corresponding_disagreements", 0)).border = border

    # --- Sheet 2: 文章清單 ---
    ws = wb.create_sheet("文章清單")
    cols = ["PMID", "年份", "標題", "期刊", "卷/期/頁", "DOI", "第一作者", "第一作者院區",
            "通訊作者", "通訊作者院區", "通訊作者來源", "PMC/OpenAlex一致性", "文章涉及院區",
            "慈濟作者數", "作者總數", "出版社", "被引次數", "作者(全部)", "PubMed連結"]
    src_fill = {"PMC全文": "C6EFCE", "OpenAlex": "DDEBF7", "PubMed e-mail": "FFF2CC", "末位作者(推定)": "F8CBAD"}
    ws.append(cols)
    for r in records:
        vip = "/".join(x for x in [r["volume"], r["issue"], r["pages"]] if x)
        ws.append([r["pmid"], int(r["year"]) if r["year"].isdigit() else r["year"], r["title"],
                   r["journal"], vip, r["doi"], r["first_author"],
                   "; ".join(r["first_author_campuses"]), "; ".join(r["corresponding_authors"]),
                   "; ".join(r["corresponding_campuses"]), r.get("corresponding_source", ""),
                   r.get("corresponding_agreement", ""), "; ".join(r["campuses"]),
                   r["n_tzuchi_authors"], r["n_authors"], r.get("publisher", ""), r.get("citations"),
                   "; ".join(a["name"] for a in r["authors"]), r["pubmed_url"]])
        rr = ws.max_row
        ws.cell(row=rr, column=19).hyperlink = r["pubmed_url"]
        ws.cell(row=rr, column=19).font = Font(color="0563C1", underline="single")
        if r.get("corresponding_source") in src_fill:
            ws.cell(row=rr, column=11).fill = PatternFill("solid", fgColor=src_fill[r["corresponding_source"]])
        if r.get("corresponding_agreement") == "不一致":
            ws.cell(row=rr, column=12).fill = PatternFill("solid", fgColor="F8CBAD")
        if r["doi"]:
            ws.cell(row=rr, column=6).hyperlink = f"https://doi.org/{r['doi']}"
            ws.cell(row=rr, column=6).font = Font(color="0563C1", underline="single")
    style_header(ws, len(cols), [10, 6, 60, 30, 14, 26, 18, 18, 22, 18, 14, 12, 28, 8, 8, 22, 8, 50, 36])

    # --- Sheet 3: 作者與隸屬 ---
    ws = wb.create_sheet("作者與隸屬")
    cols = ["PMID", "年份", "順位", "作者", "ORCID", "第一作者", "通訊作者", "通訊作者來源",
            "PMC全文標記", "OpenAlex標記", "院區分類", "隸屬機構(原文)"]
    ws.append(cols)
    for r in records:
        for a in r["authors"]:
            for aff in (a["affiliations"] or [""]):
                cls = classify_affiliation(aff)
                ws.append([r["pmid"], int(r["year"]) if r["year"].isdigit() else r["year"], a["position"],
                           a["name"], a.get("orcid", ""), "Y" if a["is_first"] else "",
                           "Y" if a["is_corresponding"] else "",
                           r.get("corresponding_source", "") if a["is_corresponding"] else "",
                           "Y" if a.get("corr_pmc") else "", "Y" if a.get("corr_openalex") else "",
                           "; ".join(cls), aff])
                if a.get("orcid"):
                    ws.cell(row=ws.max_row, column=5).hyperlink = f"https://orcid.org/{a['orcid']}"
                if cls:
                    ws.cell(row=ws.max_row, column=11).fill = PatternFill("solid", fgColor=campus_fill.get(cls[0], "FFFFFF"))
    style_header(ws, len(cols), [10, 6, 6, 22, 20, 8, 8, 14, 10, 10, 26, 90])

    # --- Sheet 4: 院區×年份 ---
    ws = wb.create_sheet("院區x年份")
    years = sorted({r["year"] for r in records if r["year"]})
    ws.append(["院區 / 單位"] + years + ["合計"])
    for c in CAMPUS_ORDER:
        counts = [sum(1 for r in records if r["year"] == y and c in r["campuses"]) for y in years]
        if sum(counts):
            ws.append([c] + counts + [sum(counts)])
            ws.cell(row=ws.max_row, column=1).fill = PatternFill("solid", fgColor=campus_fill.get(c, "FFFFFF"))
    style_header(ws, len(years) + 2, [30] + [8] * (len(years) + 1))

    # --- Sheet 5: 期刊 ---
    ws = wb.create_sheet("期刊統計")
    ws.append(["期刊", "文章數"])
    for j in sorted(Counter(r["journal"] for r in records).items(), key=lambda x: -x[1]):
        ws.append(list(j))
    style_header(ws, 2, [60, 10])

    wb.save(DATA_DIR / "tzuchi_trinetx.xlsx")


def build_outputs(records: list[dict], meta: dict, cache: dict | None = None):
    if cache is not None:
        for r in records:
            apply_cache(r, cache)
    records = [enrich(r) for r in records]
    records.sort(key=lambda r: (r["year"], r["entrez_date"], r["pmid"]), reverse=True)
    summary = summarize(records, meta)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "articles.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    write_csvs(records)
    write_xlsx(records, summary)
    print(f"Wrote {len(records)} articles to {DATA_DIR}")
    return records, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", help="rebuild outputs from an existing articles.json (no network)")
    ap.add_argument("--xml", help="parse a local PubMed XML file instead of fetching (testing)")
    ap.add_argument("--no-external", action="store_true",
                    help="skip OpenAlex / PMC / Crossref queries (cached results are still applied)")
    args = ap.parse_args()
    cache = load_cache()

    if args.from_json:
        records = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        meta = {"count": len(records)}
        if (DATA_DIR / "summary.json").exists():
            old = json.loads((DATA_DIR / "summary.json").read_text(encoding="utf-8"))
            meta = {"count": old.get("pubmed_count", len(records)), "querytranslation": old.get("query_translation", "")}
        build_outputs(records, meta, cache)
        return
    if args.xml:
        root = ET.parse(args.xml).getroot()
        records = [parse_article(pa) for pa in root.findall("PubmedArticle")]
        if not args.no_external:
            cache = refresh_cache(records, cache)
            save_cache(cache)
        build_outputs(records, {"count": len(records)}, cache)
        return

    if requests is None:
        raise SystemExit("pip install requests openpyxl")
    print("ESearch ...", file=sys.stderr)
    pmids, meta = esearch_all(QUERY)
    print(f"  {meta['count']} hits", file=sys.stderr)
    print("EFetch ...", file=sys.stderr)
    arts = efetch_xml(pmids)
    records = [parse_article(pa) for pa in arts]
    if not args.no_external:
        print("External enrichment (OpenAlex / PMC / Crossref) ...", file=sys.stderr)
        cache = refresh_cache(records, cache)
        save_cache(cache)
    build_outputs(records, meta, cache)


if __name__ == "__main__":
    main()
