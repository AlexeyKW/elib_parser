# elib_parser

Парсер публикаций автора с `elibrary.ru` (без официального API).

## Установка

```bash
python -m pip install -r requirements.txt
```

## Запуск

### Парсинг публикаций автора

```bash
python parse_elibrary_author.py --authorid 707733
```
По умолчанию создаст файл `author_707733.csv` (кодировка `UTF-8-SIG`).

### Парсинг результатов поиска

```bash
python parse_elibrary_search.py "машинное обучение"
python parse_elibrary_search.py "geoai" --max-pages 3
```

По умолчанию CSV сохраняется с именем, совпадающим с текстом запроса (например, `geoai.csv`).

### Обогащение результатов поиска (ключевые слова и аннотация)

```bash
# Список + детали за один запуск
python parse_elibrary_search.py "geoai" --max-pages 1 --enrich

# Только обогатить существующий CSV
python parse_elibrary_search.py "geoai" --enrich-only

# Перезагрузить детали для всех публикаций
python parse_elibrary_search.py "geoai" --enrich-only --enrich-force
```

При обогащении колонка `query` сохраняется. Прогресс записывается после каждой публикации; строки с `details_fetched=1` пропускаются.

### Обогащение (ключевые слова и аннотация)

```bash
# Список + детали за один запуск
python parse_elibrary_author.py --authorid 707733 --enrich

# Только обогатить существующий CSV
python parse_elibrary_author.py --authorid 707733 --enrich-only

# Перезагрузить детали для всех публикаций
python parse_elibrary_author.py --authorid 707733 --enrich-only --enrich-force
```

При обогащении для каждой публикации открывается `item_url`. Прогресс сохраняется после каждой записи; уже обработанные строки (`details_fetched=1`) пропускаются.

## Docker-сервис

Сервис поднимает HTTP API и сохраняет CSV в локальную папку `./data` (смонтирована в контейнер как `/data`).

### Запуск

```bash
docker compose up --build
```

### Эндпоинты

- `GET /health` — проверка работоспособности
- `GET /parse/{authorid}` — запускает парсинг и **возвращает CSV файлом**
  - Кэш: если `./data/author_{authorid}.csv` уже существует, будет отдан существующий файл
  - Принудительное обновление: `?force=1`
  - Обогащение деталями: `?enrich=1` (ключевые слова и аннотация)
  - Перезагрузка деталей: `?enrich=1&enrich_force=1`
  - Также добавляет заголовки:
    - `X-Total-Found-On-Site`
    - `X-Saved-To-Csv`
    - `X-Enriched-Count` (при `enrich=1`)
    - `X-Cache-Hit` (`1` если отдан кэш, иначе `0`)
- `GET /search?q=...` — поиск публикаций и **возврат CSV файлом**
  - `?max_pages=3` — сколько страниц результатов парсить (по умолчанию 1)
  - `?force=1` — принудительно выполнить поиск заново
  - `?enrich=1` — обогатить ключевыми словами и аннотацией
  - `?enrich=1&enrich_force=1` — перезагрузить детали для всех публикаций
  - Заголовки: `X-Total-Found-On-Site`, `X-Saved-To-Csv`, `X-Enriched-Count`, `X-Cache-Hit`
- `GET /enrich_search?q=...` — обогатить существующий CSV поиска без повторного парсинга
  - `?enrich_force=1` — перезагрузить детали для всех публикаций
- `GET /enrich/{authorid}` — обогатить существующий CSV без повторного парсинга списка
  - `?enrich_force=1` — перезагрузить детали для всех публикаций
- `GET /parse_json/{authorid}` — запускает парсинг и возвращает JSON (CSV сохраняется в `./data`)
  - Поддерживает `?force=1`, `?enrich=1`, `?enrich_force=1`

Пример:

- CSV: `http://localhost:8000/parse/707733`
- CSV (force): `http://localhost:8000/parse/707733?force=1`
- CSV (enrich): `http://localhost:8000/parse/707733?enrich=1`
- Enrich only: `http://localhost:8000/enrich/707733`
- JSON: `http://localhost:8000/parse_json/707733`
- JSON (force): `http://localhost:8000/parse_json/707733?force=1`

## Что извлекается

- ссылка на публикацию `/item.asp?id=...`
- название (из `span` внутри ссылки)
- авторы (из блока `font > i`)
- тип/описание (например, текст свидетельства) из `font` рядом
- журнал/номер (если есть ссылки `/contents.asp?id=...` и `/contents.asp?...&selid=...`)
- ключевые слова (при `--enrich` / `?enrich=1`, из ссылок `/keyword_items.asp?id`)
- аннотация (при обогащении, из `div#abstract1`)

## Примечание про доступ

`elibrary.ru` может отдавать страницу “Ошибка в параметрах…” / “закончилась сессия” вне браузера. Скрипт делает “прогрев” cookies заходом на главную страницу, но при капче/ограничениях может понадобиться запуск из сети/окружения, где страница автора открывается в браузере.

