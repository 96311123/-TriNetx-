"""
External enrichment for corresponding authors, ORCID, publisher and citations.

Sources (queried in this order, results cached in cache/enrich_cache.json):
  1. OpenAlex   — authorships[].is_corresponding, ORCID, PMCID (batch by PMID)
  2. PMC JATS   — full-text front matter (Europe PMC, fallback NCBI efetch db=pmc):
                  <contrib corresp="yes">, <xref ref-type="corresp|author-notes">,
                  <author-notes><corresp|fn> with e-mail
  3. Crossref   — publisher, is-referenced-by-count, ORCID
                  (Crossref has no corresponding-author field)

Corresponding-author precedence used by fetch_pubmed.enrich():
  PMC full text  >  OpenAlex  >  PubMed affiliation e-mail  >  last author
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "cache" / "enrich_cache.json"

OPENALEX = "https://api.openalex.org/works"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
NCBI_PMC = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
CROSSREF = "https://api.crossref.org/works/{doi}"

# Re-query policy (days). "found" results are kept; misses are retried.
TTL = {
    "openalex_found": 90,
    "openalex_miss": 14,
    "pmc_found": 3650,
    "pmc_miss": 30,
    "crossref": 7,      # citation counts change; refresh weekly
    "error": 0,         # always retry errors
}

CONTACT = os.getenv("CONTACT_EMAIL") or os.getenv("NCBI_EMAIL") or ""
UA = f"tzuchi-trinetx-tracker/1.0 (+https://github.com; mailto:{CONTACT})" if CONTACT else "tzuchi-trinetx-tracker/1.0"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _age_days(ts: str | None) -> float:
    if not ts:
        return 1e9
    try:
        t = dt.datetime.fromisoformat(ts)
    except ValueError:
        return 1e9
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400


def _stale(entry: dict | None, found_key: str, miss_key: str) -> bool:
    if not entry:
        return True
    st = entry.get("status")
    if st == "error":
        return True
    ttl = TTL[found_key] if st == "ok" else TTL[miss_key]
    return _age_days(entry.get("fetched_at")) > ttl


def _http_get(url: str, params: dict | None = None, accept: str | None = None):
    """Return (status_code, text). Retries on 429/5xx. Never raises."""
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.status_code, r.text
        except Exception as e:  # noqa: BLE001
            wait = 2 ** attempt
            print(f"    retry {attempt+1} {url[:70]}: {e}", file=sys.stderr)
            time.sleep(wait)
    return 0, ""


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s.lower())


def _given_initials(given: str) -> str:
    parts = re.split(r"[\s\-\.]+", given or "")
    return "".join(p[0].lower() for p in parts if p)


def match_author(authors: list[dict], surname: str = "", given: str = "", full: str = "") -> int | None:
    """Index into PubMed authors list for a name from another source."""
    if full and not surname:
        bits = full.replace(",", " ").split()
        if len(bits) >= 2:
            # "Given Middle Surname" (OpenAlex/raw) — try both orders below
            surname, given = bits[-1], " ".join(bits[:-1])
        elif bits:
            surname = bits[0]
    ns, ng = norm(surname), norm(given)
    gi = _given_initials(given)
    cands = []
    for i, a in enumerate(authors):
        if norm(a.get("last", "")) == ns and ns:
            cands.append(i)
    if not cands and full:
        # East Asian ordering "Surname Given"
        bits = full.replace(",", " ").split()
        if len(bits) >= 2:
            ns2, ng2 = norm(bits[0]), norm(" ".join(bits[1:]))
            cands = [i for i, a in enumerate(authors) if norm(a.get("last", "")) == ns2]
            ng, gi = ng2, _given_initials(" ".join(bits[1:]))
    if not cands:
        # whole-name containment (handles collective / odd splits)
        nf = norm(full or f"{given}{surname}")
        for i, a in enumerate(authors):
            if nf and (norm(a.get("fore", "") + a.get("last", "")) == nf or norm(a.get("last", "") + a.get("fore", "")) == nf):
                return i
        return None
    if len(cands) == 1:
        return cands[0]
    # disambiguate on given name, then initials
    for i in cands:
        if ng and norm(authors[i].get("fore", "")) == ng:
            return i
    for i in cands:
        if gi and _given_initials(authors[i].get("fore", "")) == gi:
            return i
    for i in cands:
        if gi and _given_initials(authors[i].get("fore", ""))[:1] == gi[:1]:
            return i
    return cands[0]


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------
def fetch_openalex(pmids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    params_base = {"select": "id,doi,ids,authorships,cited_by_count", "per-page": 100}
    if CONTACT:
        params_base["mailto"] = CONTACT
    if os.getenv("OPENALEX_API_KEY"):
        params_base["api_key"] = os.environ["OPENALEX_API_KEY"]
    for i in range(0, len(pmids), 50):
        chunk = pmids[i:i + 50]
        params = dict(params_base, filter="pmid:" + "|".join(chunk))
        code, text = _http_get(OPENALEX, params)
        if code != 200:
            for p in chunk:
                out[p] = {"status": "error", "fetched_at": _now(), "http": code}
            continue
        try:
            results = json.loads(text).get("results", [])
        except json.JSONDecodeError:
            results = []
        seen = set()
        for w in results:
            pm = re.sub(r"\D", "", str((w.get("ids") or {}).get("pmid", "")))
            if not pm:
                continue
            seen.add(pm)
            out[pm] = parse_openalex_work(w)
        for p in chunk:
            if p not in seen:
                out[p] = {"status": "miss", "fetched_at": _now()}
        time.sleep(0.15)
        print(f"  OpenAlex {min(i+50, len(pmids))}/{len(pmids)}", file=sys.stderr)
    return out


def parse_openalex_work(w: dict) -> dict:
    auths = []
    for a in w.get("authorships", []) or []:
        au = a.get("author") or {}
        auths.append({
            "name": a.get("raw_author_name") or au.get("display_name") or "",
            "display_name": au.get("display_name") or "",
            "orcid": (au.get("orcid") or "").replace("https://orcid.org/", ""),
            "position": a.get("author_position"),
            "is_corresponding": bool(a.get("is_corresponding")),
        })
    pmcid = str((w.get("ids") or {}).get("pmcid") or "")
    # OpenAlex gives e.g. "https://www.ncbi.nlm.nih.gov/pmc/articles/12821341" (no "PMC" prefix)
    m = re.search(r"(?:PMC)?(\d{4,})/?$", pmcid.rstrip("/"))
    has_corr = any(a["is_corresponding"] for a in auths)
    return {
        "status": "ok" if has_corr else "nocorr",
        "fetched_at": _now(),
        "openalex_id": (w.get("id") or "").replace("https://openalex.org/", ""),
        "pmcid": f"PMC{m.group(1)}" if m else "",
        "cited_by_count": w.get("cited_by_count"),
        "authors": auths,
    }


# ---------------------------------------------------------------------------
# PMC JATS
# ---------------------------------------------------------------------------
CORR_WORD = re.compile(r"correspond|contact|reprint", re.I)


def _strip_ns(root: ET.Element):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_jats_corresponding(xml_text: str) -> dict:
    """Return {"status", "corresp": [{"surname","given","email"}], "note"}."""
    try:
        root = _strip_ns(ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text))
    except ET.ParseError:
        return {"status": "error", "corresp": [], "note": "parse error"}
    front = root.find(".//front") if root.tag != "front" else root
    if front is None:
        front = root
    meta = front.find(".//article-meta")
    if meta is None:
        meta = front

    # id -> element for author-notes / corresp / fn
    notes: dict[str, ET.Element] = {}
    for el in meta.iter():
        if el.tag in ("corresp", "fn") and el.get("id"):
            notes[el.get("id")] = el

    def note_is_corr(el: ET.Element) -> bool:
        if el is None:
            return False
        if el.tag == "corresp":
            return True
        txt = "".join(el.itertext())
        return el.find(".//email") is not None or bool(CORR_WORD.search(txt))

    def note_email(el: ET.Element | None) -> str:
        if el is None:
            return ""
        e = el.find(".//email")
        if e is not None and e.text:
            return e.text.strip()
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", "".join(el.itertext()))
        return m.group(0).rstrip(".") if m else ""

    contribs = []
    for c in meta.iter("contrib"):
        ct = c.get("contrib-type")
        if ct and ct != "author":
            continue
        nm = c.find("name")
        if nm is None:
            nm = c.find(".//name")
        if nm is None:
            continue
        sur = "".join(nm.find("surname").itertext()).strip() if nm.find("surname") is not None else ""
        giv = "".join(nm.find("given-names").itertext()).strip() if nm.find("given-names") is not None else ""
        is_corr, email = False, ""
        if (c.get("corresp") or "").lower() == "yes":
            is_corr = True
        own_email = c.find("email")
        if own_email is not None and own_email.text:
            is_corr, email = True, own_email.text.strip()
        for x in c.findall("xref"):
            rt = x.get("ref-type", "")
            tgt = notes.get(x.get("rid", ""))
            if rt == "corresp":
                is_corr = True
                email = email or note_email(tgt)
            elif rt in ("author-notes", "fn") and tgt is not None and note_is_corr(tgt):
                is_corr = True
                email = email or note_email(tgt)
        contribs.append({"surname": sur, "given": giv, "email": email, "is_corr": is_corr})

    corr = [c for c in contribs if c["is_corr"]]
    note = "contrib-flag"
    if not corr and contribs:
        # Fallback: corresp / author-notes text that names an author
        texts = []
        for el in meta.iter():
            if el.tag == "corresp" or (el.tag == "fn" and note_is_corr(el)):
                texts.append(("".join(el.itertext()), note_email(el)))
        for txt, em in texts:
            nt = norm(txt)
            for c in contribs:
                full1 = norm(c["given"] + c["surname"])
                full2 = norm(c["surname"] + c["given"])
                if (full1 and full1 in nt) or (full2 and full2 in nt):
                    if not c["is_corr"]:
                        c["is_corr"], c["email"] = True, em
        corr = [c for c in contribs if c["is_corr"]]
        note = "author-notes-text"
    return {
        "status": "ok" if corr else "nocorr",
        "corresp": [{"surname": c["surname"], "given": c["given"], "email": c["email"]} for c in corr],
        "n_contribs": len(contribs),
        "note": note if corr else "",
    }


def fetch_pmc(pmcid: str) -> dict:
    url = EUROPEPMC.format(pmcid=pmcid)
    code, text = _http_get(url, accept="application/xml")
    src = "europepmc"
    if code != 200 or "<article" not in text:
        params = {"db": "pmc", "id": pmcid.replace("PMC", ""), "retmode": "xml", "tool": "tzuchi-trinetx-tracker"}
        if os.getenv("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        if CONTACT:
            params["email"] = CONTACT
        code, text = _http_get(NCBI_PMC, params)
        src = "ncbi-pmc"
    if code != 200 or "<article" not in text:
        return {"status": "error" if code == 0 else "miss", "fetched_at": _now(), "pmcid": pmcid, "http": code}
    res = parse_jats_corresponding(text)
    res.update({"fetched_at": _now(), "pmcid": pmcid, "source": src})
    return res


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------
def fetch_crossref(doi: str) -> dict:
    code, text = _http_get(CROSSREF.format(doi=requests.utils.quote(doi, safe="/")))
    if code == 404:
        return {"status": "miss", "fetched_at": _now()}
    if code != 200:
        return {"status": "error", "fetched_at": _now(), "http": code}
    try:
        msg = json.loads(text)["message"]
    except (json.JSONDecodeError, KeyError):
        return {"status": "error", "fetched_at": _now()}
    return parse_crossref_message(msg)


def parse_crossref_message(msg: dict) -> dict:
    auths = []
    for a in msg.get("author", []) or []:
        auths.append({
            "family": a.get("family", ""), "given": a.get("given", ""),
            "orcid": re.sub(r"^https?://orcid.org/", "", a.get("ORCID", "") or ""),
            "sequence": a.get("sequence", ""),
        })
    return {
        "status": "ok", "fetched_at": _now(),
        "publisher": msg.get("publisher", ""),
        "citations": msg.get("is-referenced-by-count"),
        "type": msg.get("type", ""),
        "license": [(l.get("URL") or "") for l in (msg.get("license") or [])][:1],
        "authors": auths,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def load_cache(path: Path = CACHE_PATH) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"openalex": {}, "pmc": {}, "crossref": {}}


def save_cache(cache: dict, path: Path = CACHE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def refresh_cache(records: list[dict], cache: dict, sources=("openalex", "pmc", "crossref")) -> dict:
    if requests is None:
        print("requests not installed; skipping external enrichment", file=sys.stderr)
        return cache
    for k in ("openalex", "pmc", "crossref"):
        cache.setdefault(k, {})

    if "openalex" in sources:
        todo = [r["pmid"] for r in records
                if _stale(cache["openalex"].get(r["pmid"]), "openalex_found", "openalex_miss")]
        print(f"OpenAlex: {len(todo)} to query", file=sys.stderr)
        if todo:
            cache["openalex"].update(fetch_openalex(todo))

    if "pmc" in sources:
        todo = []
        for r in records:
            pmcid = r.get("pmc") or (cache["openalex"].get(r["pmid"]) or {}).get("pmcid", "")
            if pmcid and _stale(cache["pmc"].get(r["pmid"]), "pmc_found", "pmc_miss"):
                todo.append((r["pmid"], pmcid))
        print(f"PMC full text: {len(todo)} to query", file=sys.stderr)
        for n, (pm, pmcid) in enumerate(todo, 1):
            cache["pmc"][pm] = fetch_pmc(pmcid)
            time.sleep(0.2)
            if n % 10 == 0:
                print(f"  PMC {n}/{len(todo)}", file=sys.stderr)

    if "crossref" in sources:
        todo = [r for r in records if r.get("doi")
                and (lambda e: not e or e.get("status") == "error" or _age_days(e.get("fetched_at")) > TTL["crossref"])(cache["crossref"].get(r["pmid"]))]
        print(f"Crossref: {len(todo)} to query", file=sys.stderr)
        for n, r in enumerate(todo, 1):
            cache["crossref"][r["pmid"]] = fetch_crossref(r["doi"])
            time.sleep(0.12)
            if n % 20 == 0:
                print(f"  Crossref {n}/{len(todo)}", file=sys.stderr)
    return cache


def apply_cache(rec: dict, cache: dict) -> dict:
    """Attach external evidence to a parsed PubMed record (in place).

    Sets per author: corr_pmc, corr_openalex, orcid (if missing)
    Sets on record:  ext = {pmc_status, openalex_status, crossref fields}
    """
    authors = rec["authors"]
    pm = rec["pmid"]
    ext = {"pmc_status": "", "openalex_status": "", "publisher": "", "citations": None,
           "openalex_citations": None, "pmcid_used": "", "unmatched": []}
    for a in authors:
        a["corr_pmc"] = False
        a["corr_openalex"] = False
        a.pop("corr_email", None)
        a["orcid"] = a.get("orcid_pubmed", "")

    oa = cache.get("openalex", {}).get(pm)
    if oa:
        ext["openalex_status"] = oa.get("status", "")
        ext["openalex_citations"] = oa.get("cited_by_count")
        oa_auths = oa.get("authors", [])
        same_len = len(oa_auths) == len(authors)
        for j, x in enumerate(oa_auths):
            idx = match_author(authors, full=x.get("name") or x.get("display_name", ""))
            if idx is None and x.get("display_name"):
                idx = match_author(authors, full=x["display_name"])
            if idx is None and same_len:
                idx = j
            if idx is None:
                if x.get("is_corresponding"):
                    ext["unmatched"].append(f"OpenAlex:{x.get('name')}")
                continue
            if x.get("is_corresponding"):
                authors[idx]["corr_openalex"] = True
            if x.get("orcid") and not authors[idx].get("orcid"):
                authors[idx]["orcid"] = x["orcid"]

    pmc = cache.get("pmc", {}).get(pm)
    if pmc:
        ext["pmc_status"] = pmc.get("status", "")
        ext["pmcid_used"] = pmc.get("pmcid", "")
        for c in pmc.get("corresp", []):
            idx = match_author(authors, surname=c.get("surname", ""), given=c.get("given", ""))
            if idx is None:
                ext["unmatched"].append(f"PMC:{c.get('given')} {c.get('surname')}")
                continue
            authors[idx]["corr_pmc"] = True
            if c.get("email"):
                authors[idx]["corr_email"] = c["email"]

    cr = cache.get("crossref", {}).get(pm)
    if cr and cr.get("status") == "ok":
        ext["publisher"] = cr.get("publisher", "")
        ext["citations"] = cr.get("citations")
        for x in cr.get("authors", []):
            if not x.get("orcid"):
                continue
            idx = match_author(authors, surname=x.get("family", ""), given=x.get("given", ""))
            if idx is not None and not authors[idx].get("orcid"):
                authors[idx]["orcid"] = x["orcid"]

    rec["ext"] = ext
    return rec
