import argparse
import csv
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

try:
    # Uses libcurl + browser-like TLS fingerprinting (often required for elibrary.ru).
    from curl_cffi import requests as crequests  # type: ignore
except Exception:  # pragma: no cover
    crequests = None

import requests


BASE_URL = "https://elibrary.ru/"


ACCESS_ERROR_MARKERS = (
    "Ошибка в параметрах страницы",
    "недостаточно прав",
    "закончилась текущая сессия",
)


USER_AGENTS = [
    # A small rotating set; eLibrary is sensitive to bot-like traffic.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


CSV_FIELDNAMES = [
    "author_id",
    "item_id",
    "item_url",
    "title",
    "authors",
    "certificate_or_type",
    "journal_name",
    "journal_url",
    "issue",
    "issue_url",
    "keywords",
    "abstract",
    "details_fetched",
]


@dataclass(frozen=True)
class Publication:
    author_id: str
    item_id: Optional[str]
    item_url: str
    title: str
    authors: str
    certificate_or_type: str
    journal_name: str
    journal_url: str
    issue: str
    issue_url: str
    keywords: str = ""
    abstract: str = ""
    details_fetched: str = ""


def _sleep_polite(min_s: float, max_s: float) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _make_session():
    """
    Returns a session-like object with .get().

    Prefer curl_cffi (Chrome impersonation) because elibrary.ru frequently
    resets connections for Python/requests TLS fingerprints.
    """
    if crequests is not None:
        s = crequests.Session(impersonate="chrome")
    else:
        s = requests.Session()
    s.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "keep-alive",
        }
    )
    return s


def _decode_response_text(r) -> str:
    enc = getattr(r, "encoding", None)
    if not enc or str(enc).lower() == "iso-8859-1":
        try:
            r.encoding = "windows-1251"
        except Exception:
            pass
    return r.text


def _fetch_html(session, url: str, *, timeout_s: int = 30) -> str:
    r = session.get(url, timeout=timeout_s)
    # curl_cffi uses .ok and .status_code; requests has raise_for_status.
    if hasattr(r, "raise_for_status"):
        r.raise_for_status()
    else:
        if getattr(r, "status_code", 0) >= 400:
            raise RuntimeError(f"HTTP {getattr(r, 'status_code', '???')} for {url}")

    return _decode_response_text(r)


def _post_html(
    session,
    url: str,
    data: dict[str, str],
    *,
    referer: str,
    timeout_s: int = 30,
) -> str:
    r = session.post(
        url,
        data=data,
        timeout=timeout_s,
        headers={"Referer": referer},
    )
    if hasattr(r, "raise_for_status"):
        r.raise_for_status()
    else:
        if getattr(r, "status_code", 0) >= 400:
            raise RuntimeError(f"HTTP {getattr(r, 'status_code', '???')} for {url}")
    return _decode_response_text(r)


def _looks_like_access_error(html: str) -> bool:
    h = html.lower()
    return any(marker.lower() in h for marker in ACCESS_ERROR_MARKERS)


def _extract_total_found(html: str) -> Optional[int]:
    soup = BeautifulSoup(html, "lxml")
    txt = soup.get_text(" ", strip=True)
    # Example: "Всего найдено 225 публикаций ..."
    m = re.search(r"Всего\s+найдено\s+(\d+)", txt, re.I)
    return int(m.group(1)) if m else None


def _extract_results_form_data(soup: BeautifulSoup) -> dict[str, str]:
    """
    Extract POST payload from form `results`.

    eLibrary pagination uses javascript:goto_page(N), which submits this form
    with updated `pagenum` rather than changing the GET query string.
    """
    frm = soup.find("form", attrs={"name": "results"})
    if not frm:
        raise RuntimeError("Не найдена форма `results`, необходимая для пагинации.")

    data: dict[str, str] = {}
    for inp in frm.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        inp_type = (inp.get("type") or "").lower()
        if inp_type in ("checkbox", "radio", "submit", "button", "image"):
            continue
        data[name] = inp.get("value", "")

    for sel in frm.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        selected = sel.find("option", selected=True) or sel.find("option")
        data[name] = selected.get("value", "") if selected else ""

    return data


def _page_range_start(html: str) -> Optional[int]:
    m = re.search(
        r"Показано на данной странице:\s*с\s*<b>\s*(\d+)\s*</b>",
        html,
        re.I,
    )
    return int(m.group(1)) if m else None


def _validate_page_html(html: str, expected_page: int, *, page_size: int = 100) -> bool:
    soup = BeautifulSoup(html, "lxml")
    pagenum_input = soup.find("input", attrs={"name": "pagenum"})
    if not pagenum_input:
        return False
    if str(pagenum_input.get("value", "")).strip() != str(expected_page):
        return False

    expected_start = (expected_page - 1) * page_size + 1
    actual_start = _page_range_start(html)
    if actual_start is not None and actual_start != expected_start:
        return False

    return soup.find("table", id="restab") is not None


def _discover_page_count(soup: BeautifulSoup) -> int:
    pages = soup.find("div", id="pages")
    if not pages:
        return 1
    nums: list[int] = []
    for a in pages.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"goto_page\((\d+)\)", href)
        if m:
            nums.append(int(m.group(1)))
        else:
            # Sometimes the number is plain text inside <a>.
            try:
                t = a.get_text(strip=True)
                if t.isdigit():
                    nums.append(int(t))
            except Exception:
                pass
    return max(nums) if nums else 1


def _clean_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def _parse_item_id(item_url: str) -> Optional[str]:
    try:
        u = urlparse(item_url)
        q = parse_qs(u.query)
        v = q.get("id", [None])[0]
        return v
    except Exception:
        return None


def _parse_publications_from_html(author_id: str, html: str) -> list[Publication]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="restab")
    if not table:
        return []

    pubs: list[Publication] = []
    for tr in table.find_all("tr"):
        # Find the main article link inside the row.
        a_item = tr.find("a", href=re.compile(r"^/item\.asp\?id=\d+"))
        if not a_item:
            continue

        item_url = urljoin(BASE_URL, a_item.get("href", "").strip())
        title = ""
        span = a_item.find("span")
        if span:
            title = _clean_space(span.get_text(" ", strip=True))
        else:
            title = _clean_space(a_item.get_text(" ", strip=True))

        # Authors appear nearby as <font><i>...</i></font>
        i_auth = tr.find("i")
        authors = _clean_space(i_auth.get_text(" ", strip=True)) if i_auth else ""

        # Journal / issue links are /contents.asp?id=... and /contents.asp?id=...&selid=...
        journal_name = journal_url = issue = issue_url = ""
        contents_links = tr.find_all("a", href=re.compile(r"^/contents\.asp\?id=\d+"))
        if contents_links:
            # Heuristic: first is journal, second with selid is issue
            for a in contents_links:
                href = a.get("href", "")
                text = _clean_space(a.get_text(" ", strip=True))
                if "selid=" in href and not issue:
                    issue = text
                    issue_url = urljoin(BASE_URL, href)
                elif not journal_name:
                    journal_name = text
                    journal_url = urljoin(BASE_URL, href)

        # Certificate/type usually sits in <font> text (excluding authors in <i>).
        certificate_or_type = ""
        fonts = tr.find_all("font")
        font_texts: list[str] = []
        for f in fonts:
            # Skip the authors font that contains <i>
            if f.find("i"):
                continue
            t = _clean_space(f.get_text(" ", strip=True))
            if t:
                font_texts.append(t)
        if font_texts:
            certificate_or_type = " ".join(font_texts)
        certificate_or_type = _clean_space(certificate_or_type)

        pubs.append(
            Publication(
                author_id=str(author_id),
                item_id=_parse_item_id(item_url),
                item_url=item_url,
                title=title,
                authors=authors,
                certificate_or_type=certificate_or_type,
                journal_name=journal_name,
                journal_url=journal_url,
                issue=issue,
                issue_url=issue_url,
            )
        )

    return pubs


def _parse_item_details(html: str) -> tuple[str, str]:
    """Extract keywords and abstract from an item.asp page."""
    soup = BeautifulSoup(html, "lxml")
    keywords: list[str] = []
    abstract = ""

    for tr in soup.find_all("tr"):
        font = tr.find("font")
        if not font:
            continue
        label = _clean_space(font.get_text(" ", strip=True))
        nxt = tr.find_next_sibling("tr")
        if not nxt:
            continue
        if "КЛЮЧЕВЫЕ СЛОВА" in label:
            keywords = [
                _clean_space(a.get_text(" ", strip=True))
                for a in nxt.find_all("a", href=re.compile(r"keyword_items\.asp"))
            ]
        elif "АННОТАЦИЯ" in label:
            div = nxt.find("div", id="abstract1")
            if div:
                p = div.find("p")
                abstract = _clean_space((p or div).get_text(" ", strip=True))

    return "; ".join(keywords), abstract


def _publication_to_row(p: Publication) -> dict[str, str]:
    return {
        "author_id": p.author_id,
        "item_id": p.item_id or "",
        "item_url": p.item_url,
        "title": p.title,
        "authors": p.authors,
        "certificate_or_type": p.certificate_or_type,
        "journal_name": p.journal_name,
        "journal_url": p.journal_url,
        "issue": p.issue,
        "issue_url": p.issue_url,
        "keywords": p.keywords,
        "abstract": p.abstract,
        "details_fetched": p.details_fetched,
    }


def _publication_from_row(row: dict[str, str]) -> Publication:
    return Publication(
        author_id=row.get("author_id", ""),
        item_id=row.get("item_id") or None,
        item_url=row.get("item_url", ""),
        title=row.get("title", ""),
        authors=row.get("authors", ""),
        certificate_or_type=row.get("certificate_or_type", ""),
        journal_name=row.get("journal_name", ""),
        journal_url=row.get("journal_url", ""),
        issue=row.get("issue", ""),
        issue_url=row.get("issue_url", ""),
        keywords=row.get("keywords", ""),
        abstract=row.get("abstract", ""),
        details_fetched=row.get("details_fetched", ""),
    )


def load_csv(path: str) -> list[Publication]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [_publication_from_row(row) for row in reader]


def _warmup_session(session) -> None:
    _fetch_html(session, BASE_URL)
    _sleep_polite(0.6, 1.2)


def enrich_publications(
    pubs: list[Publication],
    session,
    *,
    skip_fetched: bool = True,
    save_path: Optional[str] = None,
) -> tuple[int, int]:
    """
    Fetch keywords and abstract for each publication via item_url.

    Returns (enriched_count, skipped_count).
    If save_path is set, writes CSV after each successful item fetch (resume-friendly).
    """
    enriched = 0
    skipped = 0
    result = list(pubs)

    for i, p in enumerate(result):
        if skip_fetched and p.details_fetched == "1":
            skipped += 1
            continue
        if not p.item_url:
            skipped += 1
            continue

        _sleep_polite(0.7, 1.6)
        html = _fetch_html(session, p.item_url)
        if _looks_like_access_error(html):
            raise RuntimeError(
                f"eLibrary вернул ошибку доступа при загрузке {p.item_url}."
            )

        keywords, abstract = _parse_item_details(html)
        result[i] = Publication(
            author_id=p.author_id,
            item_id=p.item_id,
            item_url=p.item_url,
            title=p.title,
            authors=p.authors,
            certificate_or_type=p.certificate_or_type,
            journal_name=p.journal_name,
            journal_url=p.journal_url,
            issue=p.issue,
            issue_url=p.issue_url,
            keywords=keywords,
            abstract=abstract,
            details_fetched="1",
        )
        enriched += 1

        if save_path:
            save_csv(save_path, result)

    return enriched, skipped


def enrich_csv(
    path: str,
    *,
    skip_fetched: bool = True,
    force: bool = False,
) -> tuple[int, int, int]:
    """
    Enrich an existing CSV with keywords and abstract.

    Returns (total_rows, enriched_count, skipped_count).
    """
    pubs = load_csv(path)
    if not pubs:
        return 0, 0, 0

    if force:
        pubs = [
            Publication(
                author_id=p.author_id,
                item_id=p.item_id,
                item_url=p.item_url,
                title=p.title,
                authors=p.authors,
                certificate_or_type=p.certificate_or_type,
                journal_name=p.journal_name,
                journal_url=p.journal_url,
                issue=p.issue,
                issue_url=p.issue_url,
                keywords=p.keywords,
                abstract=p.abstract,
                details_fetched="",
            )
            for p in pubs
        ]

    session = _make_session()
    _warmup_session(session)
    enriched, skipped = enrich_publications(
        pubs,
        session,
        skip_fetched=skip_fetched,
        save_path=path,
    )
    return len(pubs), enriched, skipped


def _fetch_author_page(
    session,
    *,
    page_num: int,
    form_data: dict[str, str],
    referer: str,
    max_attempts: int = 4,
) -> str:
    post_url = urljoin(BASE_URL, "author_items.asp")

    for attempt in range(1, max_attempts + 1):
        payload = dict(form_data)
        payload["pagenum"] = str(page_num)
        html = _post_html(session, post_url, payload, referer=referer)
        if _looks_like_access_error(html):
            raise RuntimeError(f"eLibrary вернул ошибку доступа на странице {page_num}.")
        if _validate_page_html(html, page_num):
            return html
        if attempt < max_attempts:
            # Server can intermittently ignore pagenum; backoff helps.
            _sleep_polite(1.2, 2.8)

    raise RuntimeError(f"Не удалось получить страницу {page_num} после {max_attempts} попыток.")


def _iter_pages(
    author_id: str,
    session: requests.Session,
    *,
    first_html: Optional[str] = None,
) -> Iterable[tuple[int, str]]:
    # Warm up session for cookies.
    _fetch_html(session, BASE_URL)
    _sleep_polite(0.6, 1.2)

    first_url = urljoin(BASE_URL, f"author_items.asp?authorid={author_id}")
    # Important: first page should be fetched AFTER warmup so the session cookies
    # match the subsequent POST pagination.
    first_html = _fetch_html(session, first_url)
    if _looks_like_access_error(first_html):
        raise RuntimeError(
            "eLibrary вернул страницу с ошибкой доступа/сессии. "
            "Обычно помогает запуск из браузерной среды или с валидными cookies; "
            "также проверьте, что страница открывается в обычном браузере без капчи."
        )
    if not _validate_page_html(first_html, 1):
        raise RuntimeError("Первая страница публикаций получена в неожиданном формате.")

    soup1 = BeautifulSoup(first_html, "lxml")
    page_count = _discover_page_count(soup1)
    form_data = _extract_results_form_data(soup1)

    yield 1, first_html

    for p in range(2, page_count + 1):
        _sleep_polite(0.7, 1.6)
        html_p = _fetch_author_page(
            session,
            page_num=p,
            form_data=form_data,
            referer=first_url,
        )
        form_data = _extract_results_form_data(BeautifulSoup(html_p, "lxml"))
        yield p, html_p


def save_csv(path: str, pubs: list[Publication]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        w.writeheader()
        for p in pubs:
            w.writerow(_publication_to_row(p))


def parse_author_to_csv(
    author_id: str,
    out_path: str,
    *,
    max_runs: int = 3,
    enrich: bool = False,
    enrich_force: bool = False,
) -> tuple[Optional[int], int]:
    """
    Parses all publications for author and writes CSV.

    eLibrary иногда нестабильно обрабатывает POST-пагинацию (может игнорировать pagenum).
    Поэтому делаем несколько «полных прогонов» с новой сессией.
    """
    author_id = str(author_id).strip()
    last_err: Optional[Exception] = None

    for run in range(1, max_runs + 1):
        session = _make_session()
        all_pubs: list[Publication] = []
        seen_item_urls: set[str] = set()
        total_found: Optional[int] = None

        try:
            for page_num, html in _iter_pages(author_id, session):
                if page_num == 1:
                    total_found = _extract_total_found(html)

                pubs = _parse_publications_from_html(author_id, html)
                for p in pubs:
                    if p.item_url in seen_item_urls:
                        continue
                    seen_item_urls.add(p.item_url)
                    all_pubs.append(p)

                if page_num == 1 and not pubs:
                    raise RuntimeError(
                        "Не нашёл таблицу публикаций `#restab` или ссылки /item.asp?id=... на первой странице."
                    )

            save_csv(out_path, all_pubs)

            if enrich:
                enrich_csv(out_path, skip_fetched=not enrich_force, force=enrich_force)

            return total_found, len(all_pubs)
        except Exception as e:
            last_err = e
            if run < max_runs:
                _sleep_polite(1.5, 3.5)
                continue
            raise

    # Unreachable, but keeps type-checkers happy.
    raise RuntimeError(str(last_err) if last_err else "Unknown parse failure")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--authorid", required=True, help="ID автора eLibrary, например 707733")
    ap.add_argument("--out", default="", help="Путь к CSV. По умолчанию author_<id>.csv")
    ap.add_argument(
        "--enrich",
        action="store_true",
        help="После парсинга списка обогатить CSV ключевыми словами и аннотациями",
    )
    ap.add_argument(
        "--enrich-only",
        action="store_true",
        help="Только обогатить существующий CSV (без повторного парсинга списка)",
    )
    ap.add_argument(
        "--enrich-force",
        action="store_true",
        help="Перезагрузить детали для всех публикаций, даже если уже обогащены",
    )
    args = ap.parse_args()

    author_id = str(args.authorid).strip()
    out = args.out.strip() or f"author_{author_id}.csv"

    if args.enrich_only:
        if not os.path.exists(out):
            raise SystemExit(f"CSV не найден: {out}")
        total, enriched, skipped = enrich_csv(
            out,
            skip_fetched=not args.enrich_force,
            force=args.enrich_force,
        )
        print(f"Enriched: {enriched}, skipped: {skipped}, total: {total} ({out})")
        return 0

    total_found, saved = parse_author_to_csv(
        author_id,
        out,
        enrich=args.enrich,
        enrich_force=args.enrich_force,
    )
    if total_found is not None:
        print(f"Total found on site: {total_found}")
    print(f"Saved to CSV: {saved} ({out})")
    if args.enrich:
        print("(Details enrichment completed during parse)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

