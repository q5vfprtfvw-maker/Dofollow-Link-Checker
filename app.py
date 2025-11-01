# -*- coding: utf-8 -*-
import time
import random
import re
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
import pandas as pd
import streamlit as st
from io import StringIO

st.set_page_config(page_title="Dofollow Link Checker", page_icon="🔗")
st.title("🔗 Dofollow Link Checker")
st.write("Wgraj CSV lub XLSX z kolumnami **page_url** i **target** (domena lub pełny URL). Aplikacja sprawdzi, czy są linki dofollow.")

# Przykładowy CSV do pobrania
sample_csv = "page_url,target\nhttps://example.com/blog/post-1,mydomain.pl\nhttps://another-site.net/resources,https://mydomain.pl/oferta\n"
st.download_button("📥 Pobierz przykładowy CSV", data=sample_csv, file_name="urls_template.csv", mime="text/csv")

# Ustawienia
TIMEOUT = 25
RETRY_COUNT = 2
RETRY_BACKOFF = [2, 5]
HEADERS_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
]

def normalize_host(host: str) -> str:
    host = (host or "").lower().strip()
    if host.startswith(("http://", "https://")):
        host = urlparse(host).netloc
    if host.startswith("www."):
        host = host[4:]
    return host

def match_target(href: str, target: str) -> bool:
    """Zwraca True, jeśli href prowadzi do targetu (pełny URL albo domena).
       Działa dla http i https, bo porównujemy same hosty."""
    if not href:
        return False
    try:
        parsed = urlparse(href)
    except Exception:
        return False

    # Gdy target jest pełnym URL
    if target.startswith(("http://", "https://")):
        t = urlparse(target)
        if parsed.netloc and t.netloc and normalize_host(parsed.netloc) == normalize_host(t.netloc):
            if t.path and t.path != "/":
                return parsed.path.rstrip("/") == t.path.rstrip("/")
            else:
                return True
        return False

    # Gdy target to domena
    target_host = normalize_host(target)
    link_host = normalize_host(parsed.netloc) if parsed.netloc else ""
    return link_host.endswith(target_host) and link_host != ""

def has_page_nofollow(soup: BeautifulSoup) -> bool:
    metas = soup.find_all("meta", attrs={"name": re.compile(r"robots|googlebot", re.I)})
    for m in metas:
        content = (m.get("content") or "").lower()
        if "nofollow" in content:
            return True
    return False

def x_robots_nofollow(headers: dict) -> bool:
    for k, v in headers.items():
        if k.lower() == "x-robots-tag" and v and "nofollow" in v.lower():
            return True
    return False

def is_dofollow_link(rel_values, page_nofollow: bool) -> bool:
    if page_nofollow:
        return False
    if isinstance(rel_values, str):
        rel_list = [r.strip().lower() for r in rel_values.split() if r.strip()]
    else:
        rel_list = [str(r).lower() for r in (rel_values or [])]
    return not any(r in {"nofollow", "ugc", "sponsored"} for r in rel_list)

def fetch(url: str):
    headers = {"User-Agent": random.choice(HEADERS_LIST), "Accept-Language": "pl,en;q=0.8"}
    sess = requests.Session()
    resp = sess.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    return resp

def safe_get(url: str):
    last_exc = None
    for attempt in range(RETRY_COUNT + 1):
        try:
            return fetch(url)
        except requests.RequestException as e:
            last_exc = e
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)-1)])
    raise last_exc

# Stan - żeby wyniki nie znikały po rerunie
if "results_df" not in st.session_state:
    st.session_state.results_df = None
if "results_csv" not in st.session_state:
    st.session_state.results_csv = None

# Interfejs
uploaded = st.file_uploader("📂 Wgraj CSV lub XLSX", type=["csv", "xlsx"], key="uploader")
start_btn = st.button("🚀 Uruchom sprawdzanie", key="run")

if start_btn:
    try:
        if not uploaded:
            st.warning("Wgraj najpierw plik z kolumnami page_url i target.")
            st.stop()

        # Wczytywanie pliku - CSV/XLSX, różne separatory
        df = None
        if uploaded.name.lower().endswith((".xlsx", ".xls")):
            try:
                df = pd.read_excel(uploaded)
            except Exception as e:
                st.error(f"Nie udało się odczytać XLSX: {e}")
                st.stop()
        else:
            try:
                uploaded.seek(0)
                raw = uploaded.read()
                text = raw.decode("utf-8", errors="ignore")
                for sep in [",", ";", "\t", "|"]:
                    try:
                        tmp = pd.read_csv(StringIO(text), sep=sep)
                        if {"page_url", "target"}.issubset({c.strip() for c in tmp.columns}):
                            df = tmp
                            break
                    except Exception:
                        continue
                if df is None:
                    try:
                        tmp = pd.read_csv(StringIO(text), sep=None, engine="python")
                        if {"page_url", "target"}.issubset({c.strip() for c in tmp.columns}):
                            df = tmp
                    except Exception:
                        pass
            except Exception as e:
                st.error(f"Nie udało się wczytać CSV: {e}")
                st.stop()

        if df is None:
            st.error("❌ Nie udało się wczytać pliku. Upewnij się, że ma kolumny: page_url i target.")
            st.stop()

        required = {"page_url", "target"}
        if not required.issubset({c.strip() for c in df.columns}):
            st.error("Plik nie ma wymaganych kolumn: page_url, target")
            st.stop()

        rows = df.to_dict("records")
        if not rows:
            st.warning("Plik nie zawiera żadnych wierszy do sprawdzenia.")
            st.stop()

        results = []
        progress = st.progress(0)
        status_area = st.empty()

        for i, row in enumerate(rows, 1):
            page_url = str(row.get("page_url", "")).strip()
            target = str(row.get("target", "")).strip()
            try:
                resp = safe_get(page_url)
                final_url = resp.url
                status_code = resp.status_code
                content_type = resp.headers.get("Content-Type", "")
                if status_code >= 400 or ("text/html" not in content_type.lower() and "xml" not in content_type.lower()):
                    results.append({
                        "page_url": page_url,
                        "final_url": final_url,
                        "status_code": status_code,
                        "has_link": False,
                        "matched_links_count": 0,
                        "dofollow_links_count": 0,
                        "link_examples": "",
                        "page_nofollow": False,
                        "x_robots_nofollow": "nofollow" in (resp.headers.get("X-Robots-Tag", "").lower()),
                        "notes": "Non-HTML lub błąd HTTP",
                    })
                else:
                    soup = BeautifulSoup(resp.text, "lxml")
                    page_nofollow = has_page_nofollow(soup) or x_robots_nofollow(resp.headers)
                    anchors = soup.find_all("a", href=True)
                    matched_links, dofollow_links = [], []

                    base_url = final_url
                    for a in anchors:
                        href = a.get("href")
                        abs_href = urljoin(base_url, href)
                        if match_target(abs_href, target):
                            matched_links.append(abs_href)
                            if is_dofollow_link(a.get("rel"), page_nofollow):
                                dofollow_links.append(abs_href)

                    results.append({
                        "page_url": page_url,
                        "final_url": final_url,
                        "status_code": status_code,
                        "has_link": bool(matched_links),
                        "matched_links_count": len(matched_links),
                        "dofollow_links_count": len(dofollow_links),
                        "link_examples": "; ".join((dofollow_links or matched_links)[:3]),
                        "page_nofollow": page_nofollow,
                        "x_robots_nofollow": x_robots_nofollow(resp.headers),
                        "notes": "",
                    })

                status_area.info(f"Przetworzono {i}/{len(rows)}")
                progress.progress(int(i/len(rows)*100))
                time.sleep(0.2 + random.random()*0.3)
            except Exception as e:
                results.append({
                    "page_url": page_url,
                    "final_url": "",
                    "status_code": "",
                    "has_link": False,
                    "matched_links_count": 0,
                    "dofollow_links_count": 0,
                    "link_examples": "",
                    "page_nofollow": "",
                    "x_robots_nofollow": "",
                    "notes": f"Błąd: {type(e).__name__}: {e}",
                })

        out_df = pd.DataFrame(results)
        st.session_state.results_df = out_df
        st.session_state.results_csv = out_df.to_csv(index=False).encode("utf-8")
        st.success("✅ Gotowe. Wyniki poniżej. Jeśli nic nie widzisz, przewiń w dół.")

    except Exception as e:
        st.error(f"Niespodziewany błąd: {type(e).__name__}: {e}")

# Render wyników także po rerunie aplikacji
if st.session_state.results_df is not None:
    st.dataframe(st.session_state.results_df, use_container_width=True)
    st.download_button(
        "📊 Pobierz results_dofollow.csv",
        data=st.session_state.results_csv,
        file_name="results_dofollow.csv",
        mime="text/csv"
    )

st.caption("Uwaga: aplikacja nie renderuje JS. Linki generowane dynamicznie mogą nie zostać wykryte. Szanuj robots.txt.")
