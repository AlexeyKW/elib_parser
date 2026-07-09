import argparse
import csv
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parse_elibrary_author import (
    ACCESS_ERROR_MARKERS,
    BASE_URL,
    _clean_space,
    _extract_total_found,
    _fetch_html,
    _looks_like_access_error,
    _make_session,
    _parse_publications_from_html,
    _post_html,
    _sleep_polite,
    _warmup_session,
)


SEARCH_CSV_FIELDNAMES = [
    "query",
    "item_id",
    "item_url",
    "title",
    "authors",
    "certificate_or_type",
    "journal_name",
    "journal_url",
    "issue",
    "issue_url",
]


@dataclass(frozen=True)
class SearchPublication:
    query: str
    item_id: Optional[str]
    item_url: str
    title: str
    authors: str
    certificate_or_type: str
    journal_name: str
    journal_url: str
    issue: str
    issue_url: str


def _default_search_payload(query: str) -> dict[str, str]:
    """Default POST payload for the sidebar search form (form name=search)."""
    return {
        "where_fulltext": "on",
        "where_name": "on",
        "where_abstract": "on",
        "where_keywords": "on",
        "where_affiliation": "",
        "where_references": "",
        "type_article": "on",
        "type_disser": "on",
        "type_book": "on",
        "type_report": "on",
        "type_conf": "on",
        "type_patent": "on",
        "type_preprint": "on",
        "type_grant": "on",
        "type_dataset": "on",
        "search_freetext": "",
        "search_morph": "on",
        "search_fulltext": "",
        "search_open": "",
        "search_results": "",
        "titles_all": "",
        "authors_all": "",
        "rubrics_all": "",
        "queryboxid": "",
        "itemboxid": "",
        "begin_year": "",
        "end_year": "",
        "issues": "all",
        "orderby": "rank",
        "order": "rev",
        "changed": "1",
        "ftext": query.strip(),
    }


def _extract_search_total_found(html: str) -> Optional[int]:
    soup = BeautifulSoup(html, "lxml")
    txt = soup.get_text(" ", strip=True)
    m = re.search(r"ВСЕГО\s+НАЙДЕНО\s+ПУБЛИКАЦИЙ:\s*(\d+)", txt, re.I)
    if m:
        return int(m.group(1))
    return _extract_total_found(html)


def _discover_search_page_count(html: str) -> int:
    """
    Discover total page count from pagination block.

    Supports author-style `div#pages` with `goto_page(N)` and search-style
    links like `query_results.asp?pagenum=N`.
    """
    soup = BeautifulSoup(html, "lxml")
    nums: list[int] = []

    pages_div = soup.find("div", id="pages")
    if pages_div:
        for a in pages_div.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"goto_page\((\d+)\)", href)
            if m:
                nums.append(int(m.group(1)))
                continue
            m = re.search(r"pagenum=(\d+)", href, re.I)
            if m:
                nums.append(int(m.group(1)))
                continue
            t = _clean_space(a.get_text(" ", strip=True))
            if t.isdigit():
                nums.append(int(t))

    for a in soup.find_all("a", href=re.compile(r"pagenum=\d+", re.I)):
        m = re.search(r"pagenum=(\d+)", a["href"], re.I)
        if m:
            nums.append(int(m.group(1)))

    return max(nums) if nums else 1


def _search_publication_to_row(p: SearchPublication) -> dict[str, str]:
    return {
        "query": p.query,
        "item_id": p.item_id or "",
        "item_url": p.item_url,
        "title": p.title,
        "authors": p.authors,
        "certificate_or_type": p.certificate_or_type,
        "journal_name": p.journal_name,
        "journal_url": p.journal_url,
        "issue": p.issue,
        "issue_url": p.issue_url,
    }


def _parse_search_publications_from_html(query: str, html: str) -> list[SearchPublication]:
    pubs = _parse_publications_from_html("", html)
    return [
        SearchPublication(
            query=query,
            item_id=p.item_id,
            item_url=p.item_url,
            title=p.title,
            authors=p.authors,
            certificate_or_type=p.certificate_or_type,
            journal_name=p.journal_name,
            journal_url=p.journal_url,
            issue=p.issue,
            issue_url=p.issue_url,
        )
        for p in pubs
    ]


def _query_to_csv_path(query: str, out_path: Optional[str] = None) -> str:
    if out_path:
        return out_path
    name = query.strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.strip(". ")
    if not name:
        name = "search"
    if len(name) > 200:
        name = name[:200]
    return f"{name}.csv"


def save_search_csv(path: str, pubs: list[SearchPublication]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=SEARCH_CSV_FIELDNAMES)
        w.writeheader()
        for p in pubs:
            w.writerow(_search_publication_to_row(p))


def _submit_search(session, query: str) -> str:
    if len(query.strip()) < 2:
        raise ValueError("Поисковый запрос должен содержать не менее двух символов.")

    post_url = urljoin(BASE_URL, "query_results.asp")
    payload = _default_search_payload(query)
    html = _post_html(session, post_url, payload, referer=BASE_URL)
    if _looks_like_access_error(html):
        raise RuntimeError(
            "eLibrary вернул страницу с ошибкой доступа/сессии при выполнении поиска. "
            "Обычно помогает запуск из сети, где elibrary.ru открывается в браузере."
        )
    return html


def _fetch_search_page(session, page_num: int, *, referer: str) -> str:
    if page_num <= 1:
        raise ValueError("page_num must be >= 2 for pagination fetch")
    page_url = urljoin(BASE_URL, f"query_results.asp?pagenum={page_num}")
    html = _fetch_html(session, page_url)
    if _looks_like_access_error(html):
        raise RuntimeError(f"eLibrary вернул ошибку доступа на странице {page_num}.")
    return html


def _iter_search_pages(
    session,
    query: str,
    *,
    max_pages: int,
) -> Iterable[tuple[int, str]]:
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")

    _warmup_session(session)

    first_html = _submit_search(session, query)
    if not BeautifulSoup(first_html, "lxml").find("table", id="restab"):
        if "Не найдено публикаций" in first_html:
            yield 1, first_html
            return
        raise RuntimeError(
            "Не найдена таблица результатов `#restab` на первой странице поиска."
        )

    total_pages = _discover_search_page_count(first_html)
    pages_to_fetch = min(max_pages, total_pages)
    referer = urljoin(BASE_URL, "query_results.asp")

    yield 1, first_html

    for page_num in range(2, pages_to_fetch + 1):
        _sleep_polite(0.7, 1.6)
        html = _fetch_search_page(session, page_num, referer=referer)
        if not BeautifulSoup(html, "lxml").find("table", id="restab"):
            raise RuntimeError(
                f"Не найдена таблица результатов `#restab` на странице {page_num}."
            )
        yield page_num, html


def parse_search_to_csv(
    query: str,
    out_path: Optional[str] = None,
    *,
    max_pages: int = 1,
) -> tuple[Optional[int], int]:
    """
    Выполняет поиск на elibrary.ru и сохраняет результаты в CSV.

    CSV по умолчанию называется как текст запроса (с безопасной санитизацией имени файла).
    max_pages — сколько страниц результатов парсить (начиная с первой).
    """
    query = query.strip()
    if not query:
        raise ValueError("Поисковый запрос не может быть пустым.")

    out = _query_to_csv_path(query, out_path)
    session = _make_session()

    all_pubs: list[SearchPublication] = []
    seen_item_urls: set[str] = set()
    total_found: Optional[int] = None

    for page_num, html in _iter_search_pages(session, query, max_pages=max_pages):
        if page_num == 1:
            total_found = _extract_search_total_found(html)

        pubs = _parse_search_publications_from_html(query, html)
        for p in pubs:
            if p.item_url in seen_item_urls:
                continue
            seen_item_urls.add(p.item_url)
            all_pubs.append(p)

    save_search_csv(out, all_pubs)
    return total_found, len(all_pubs)


def main() -> int:
    ap = argparse.ArgumentParser(description="Парсер результатов поиска elibrary.ru")
    ap.add_argument("query", help="Текст поискового запроса (ftext)")
    ap.add_argument(
        "--out",
        default="",
        help="Путь к CSV. По умолчанию — имя файла совпадает с текстом запроса",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Сколько страниц результатов парсить (по умолчанию 1)",
    )
    args = ap.parse_args()

    out = args.out.strip() or None
    total_found, saved = parse_search_to_csv(
        args.query,
        out,
        max_pages=max(1, args.max_pages),
    )
    csv_path = _query_to_csv_path(args.query, out)
    if total_found is not None:
        print(f"Total found on site: {total_found}")
    print(f"Saved to CSV: {saved} ({csv_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
