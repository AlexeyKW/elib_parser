# elib_parser

Парсер публикаций автора с `elibrary.ru` (без официального API).

## Установка

```bash
python -m pip install -r requirements.txt
```

## Запуск

```bash
python parse_elibrary_author.py --authorid 707733
```

По умолчанию создаст файл `author_707733.csv` (кодировка `UTF-8-SIG`, удобно для Excel).

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
  - Также добавляет заголовки:
    - `X-Total-Found-On-Site`
    - `X-Saved-To-Csv`
    - `X-Cache-Hit` (`1` если отдан кэш, иначе `0`)
- `GET /parse_json/{authorid}` — запускает парсинг и возвращает JSON (CSV сохраняется в `./data`)
  - Поддерживает `?force=1`

Пример:

- CSV: `http://localhost:8000/parse/707733`
- CSV (force): `http://localhost:8000/parse/707733?force=1`
- JSON: `http://localhost:8000/parse_json/707733`
- JSON (force): `http://localhost:8000/parse_json/707733?force=1`

## Что извлекается

- ссылка на публикацию `/item.asp?id=...`
- название (из `span` внутри ссылки)
- авторы (из блока `font > i`)
- тип/описание (например, текст свидетельства) из `font` рядом
- журнал/номер (если есть ссылки `/contents.asp?id=...` и `/contents.asp?...&selid=...`)

## Примечание про доступ

`elibrary.ru` может отдавать страницу “Ошибка в параметрах…” / “закончилась сессия” вне браузера. Скрипт делает “прогрев” cookies заходом на главную страницу, но при капче/ограничениях может понадобиться запуск из сети/окружения, где страница автора открывается в браузере.

