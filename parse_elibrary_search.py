import argparse
import csv
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parse_elibrary_author import (
    BASE_URL,
    _clean_space,
    _extract_total_found,
    _fetch_html,
    _looks_like_access_error,
    _make_session,
    _parse_item_details,
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
    "keywords",
    "abstract",
    "details_fetched",
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
    keywords: str = ""
    abstract: str = ""
    details_fetched: str = ""


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
        "keywords": p.keywords,
        "abstract": p.abstract,
        "details_fetched": p.details_fetched,
    }


def _search_publication_from_row(row: dict[str, str]) -> SearchPublication:
    return SearchPublication(
        query=row.get("query", ""),
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


def load_search_csv(path: str) -> list[SearchPublication]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [_search_publication_from_row(row) for row in reader]


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


def enrich_search_publications(
    pubs: list[SearchPublication],
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
        result[i] = SearchPublication(
            query=p.query,
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
            save_search_csv(save_path, result)

    return enriched, skipped


def enrich_search_csv(
    path: str,
    *,
    skip_fetched: bool = True,
    force: bool = False,
) -> tuple[int, int, int]:
    """
    Enrich an existing search CSV with keywords and abstract.

    Returns (total_rows, enriched_count, skipped_count).
    """
    pubs = load_search_csv(path)
    if not pubs:
        return 0, 0, 0

    if force:
        pubs = [
            SearchPublication(
                query=p.query,
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
    enriched, skipped = enrich_search_publications(
        pubs,
        session,
        skip_fetched=skip_fetched,
        save_path=path,
    )
    return len(pubs), enriched, skipped


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
    enrich: bool = False,
    enrich_force: bool = False,
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

    if enrich:
        enrich_search_csv(out, skip_fetched=not enrich_force, force=enrich_force)

    return total_found, len(all_pubs)


def main() -> int:
    ap = argparse.ArgumentParser(description="Парсер результатов поиска elibrary.ru")
    ap.add_argument("query", nargs="?", help="Текст поискового запроса (ftext)")
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

    out = args.out.strip() or None

    if args.enrich_only:
        if not args.query and not out:
            raise SystemExit("Укажите запрос или --out для --enrich-only")
        csv_path = out or _query_to_csv_path(args.query or "")
        if not os.path.exists(csv_path):
            raise SystemExit(f"CSV не найден: {csv_path}")
        total, enriched, skipped = enrich_search_csv(
            csv_path,
            skip_fetched=not args.enrich_force,
            force=args.enrich_force,
        )
        print(f"Enriched: {enriched}, skipped: {skipped}, total: {total} ({csv_path})")
        return 0

    if not args.query:
        raise SystemExit("Укажите текст поискового запроса")

    total_found, saved = parse_search_to_csv(
        args.query,
        out,
        max_pages=max(1, args.max_pages),
        enrich=args.enrich,
        enrich_force=args.enrich_force,
    )
    csv_path = _query_to_csv_path(args.query, out)
    if total_found is not None:
        print(f"Total found on site: {total_found}")
    print(f"Saved to CSV: {saved} ({csv_path})")
    if args.enrich:
        print("(Details enrichment completed during parse)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
