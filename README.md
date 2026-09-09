# Документация по доработкам Calibre-Web-Automated (CWA)

Эта папка содержит всё, что нужно, чтобы создать **ещё один контейнер CWA**
с библиотекой Calibre для другой тематики книг и на другом порту, **со всеми
сделанными улучшениями**.

---

## 1. Что было улучшено (сводка)

### 1.1. AI-извлечение метаданных (`cps/metadata_helper.py`)

При добавлении книги CWA определяет метаданные **локальной AI-моделью**
(gemma-4, OpenAI-совместимый сервер `llama-server`), а затем добирает
недостающее (аннотацию) у онлайн-провайдеров.

- `_fetch_metadata_from_ai()` — отправляет обложку/страницы модели и получает
  `title`, `authors`, `language`, `tags`, `publisher`, `cover_page`.
- **Мультистраничная проверка обложки** — рендерятся первые **3 страницы** PDF
  (`_render_first_pages`), модель отвечает `cover_page` (номер страницы);
  указанная страница сохраняется как `cover.jpg`.
- **Поддержка epub** (`_extract_epub_cover_and_meta`): обложка и метаданные
  извлекаются из OPF (zip + XML, без внешних зависимостей).
- **Поддержка fb2** (`_extract_fb2_cover_and_meta`): обложка и метаданные
  извлекаются из `<title-info>` (название, авторы, **аннотация** `<annotation>`,
  жанры, язык, издатель).
- **Извлечение аннотации** — из epub (`<dc:description>`), fb2 (`<annotation>`)
  или онлайн-провайдера (см. 1.7).
- `_normalize_language()` — нормализация языка (`Russian` → `ru` → `rus`).
- `_normalize_description()` — описание приводится к HTML (`<p>...</p>`).
- Очистка старых тегов перед добавлением новых (`book.tags.clear()`).
- Применение языка через `get_lang3` (ISO639-1 → ISO639-3).
- **Пересчёт `author_sort`** при обновлении авторов (иначе Calibre-Web пишет
  `Author 'X' not found to display name in right order`).

### 1.2. Поддержка DJVU (`scripts/ingest_processor.py`)

- `_convert_djvu_to_pdf()` — при поступлении `.djvu` конвертирует его в PDF
  через `ddjvu -format=pdf -subsample=2`.
- Скан-книги (DJVU) сохраняются **только в PDF** (epub-конвертация пропускается —
  у скана нет текстового слоя, и Calibre зависает).
- Исходный `.djvu` удаляется после конвертации.
- `djvu` добавлен в `SUPPORTED_EXT_REGEX` ingest-сервиса.
- djvulibre (`ddjvu`) ставится при старте (см. `custom-cont-init.d`).

### 1.3. Порядок импорта: PDF до метаданных (`scripts/ingest_processor.py`)

Раньше порядок был «добавить epub → метаданные → добавить PDF», поэтому AI не
мог отрендерить первую страницу PDF. Теперь при сохранении оригинала порядок:

```
add_book_to_library(epub, fetch_metadata=False)   # без метаданных
→ add_format_to_book(id, pdf)                      # добавить PDF
→ fetch_metadata_if_enabled(book_id)               # метаданные (PDF уже на месте)
```

Это позволило AI брать **цветную обложку** из первой страницы PDF.

### 1.4. Исправление обложек с Litres (`cps/helper.py`)

- `save_cover_from_url`: `allow_redirects=True` (Litres отдаёт 301 на cdn.litres.ru).
- `save_cover`: если сервер не шлёт `Content-Type`, тип определяется по URL.

### 1.5. Исправление `config` (`cps/config_sql.py`)

- В контексте ingest-процессора атрибуты конфига не инициализированы — добавлены
  `getattr(...)` для `config_calibre_split` и явная инициализация
  `config_use_google_drive` / `config_google_drive_folder` (иначе падала загрузка
  обложек от онлайн-провайдеров).

### 1.6. Настройки AI в UI/БД

- `scripts/cwa_schema.sql`, `scripts/cwa_db.py` — колонки/дефолты
  `auto_metadata_ai_enabled`, `ai_metadata_url`.
- `cps/templates/cwa_settings.html` — чекбокс «Use Local AI» + поле «AI Server URL».

### 1.7. Аннотация из онлайн-провайдера (fallback)

AI по обложке не извлекает аннотацию (её нет на обложке). Поэтому после успешного
применения AI-метаданных CWA проверяет, есть ли у книги аннотация; если нет —
идёт к онлайн-провайдерам и берёт **только описание** (`_apply_description_only`),
не трогая уже корректные название/авторов. Если аннотации нет ни у одного
провайдера — книга просто остаётся без описания (без ошибок).

### 1.8. Провайдеры метаданных

Базовый класс `cps/metadata_provider/base.py` (`BaseMetadataProvider`) и 14
провайдеров сайтов (наследуются от него через множественное наследование с
`Metadata`):

| Файл | Сайт | Тип |
|---|---|---|
| `google_books.py` | Google Books | JSON API |
| `livelib.py` | LiveLib | HTML + JSON-LD |
| `chitai_gorod.py` | Читай-город | JSON API |
| `litnet.py` | Litnet | JSON API |
| `mybook.py` | MyBook | JSON API |
| `litmarket.py` | Литмаркет | HTML |
| `eksmo.py` | Эксмо | HTML |
| `bookmix.py` | Bookmix | HTML |
| `flibusta.py` | Flibusta | HTML |
| `loveread.py` | LoveRead.ec | HTML |
| `libcat.py` | LibCat.ru | HTML |
| `bookz.py` | Bookz.ru | HTML |
| `kniga_online.py` | Kniga-online.com | HTML |
| `meloman.py` | Meloman.kz | HTML |

Плюс улучшения встроенных: `proglib.py` (теги из URL статьи), `urmp_standalone.py`
(описание + теги).

---

## 2. Локальные AI-серверы (запущены на хосте)

| Порт | Модель | Файл | Назначение |
|---|---|---|---|
| 8899 | gemma-4-E4B | `~/models/gguf_models/...gemma...gguf` | извлечение метаданных из обложки |
| 8805 | surya-2 | `~/models/gguf_models/surya/surya-2.gguf` | OCR (используется вручную, в CWA не встроен) |

**Важно:** изнутри контейнера хост доступен по адресу `http://172.17.0.1` (шлюз
Docker), а не `127.0.0.1`. Поэтому в настройке CWA `ai_metadata_url` по умолчанию
стоит `http://172.17.0.1:8899`.

> gemma-4 — «рассуждающая» модель: на коротком `max_tokens` она тратит весь лимит
> на «мысли» и возвращает пустой ответ. В `metadata_helper.py` задан
> `max_tokens: 1500` — этого достаточно.

---

## 3. Состав папки `cwa_doc`

```
cwa_doc/
├── README.md                     ← этот файл
├── docker-compose.yml            ← шаблон для нового контейнера
├── custom-cont-init.d/
│   └── 01-djvu-support.sh        ← ставит djvulibre + патчит SUPPORTED_EXT_REGEX
├── trigger_ingest.sh             ← «пробуждает» файлы в папке импорта
├── setup_new_container.sh        ← автосоздание контейнера (тематика + порт)
└── patches/                      ← изменённые файлы CWA (копировать поверх кода)
    ├── cps/
    │   ├── config_sql.py
    │   ├── helper.py
    │   ├── metadata_helper.py
    │   ├── metadata_provider/
    │   │   ├── base.py            ← базовый класс
    │   │   ├── google_books.py, livelib.py, chitai_gorod.py, litnet.py,
    │   │   │   mybook.py, litmarket.py, eksmo.py, bookmix.py,
    │   │   │   flibusta.py, loveread.py, libcat.py, bookz.py,
    │   │   │   kniga_online.py, meloman.py
    │   │   ├── proglib.py
    │   │   └── urmp_standalone.py
    │   └── templates/
    │       └── cwa_settings.html
    └── scripts/
        ├── cwa_db.py
        ├── cwa_schema.sql
        └── ingest_processor.py
```

---

## 4. Создание нового контейнера (пошагово)

> **Быстрый путь** — автоматический скрипт, выполняющий все шаги ниже:
>
> ```bash
> ~/data/calibre/cwa_doc/setup_new_container.sh <тематика> <порт>
> # пример:
> ~/data/calibre/cwa_doc/setup_new_container.sh math 1232
> ```

### Шаг 1. Каталог проекта

```bash
mkdir -p ~/data/.app/calibre/books_topic
cd ~/data/.app/calibre/books_topic
```

(название `books_topic` — любое; ниже подразумевается тематика, например `math`,
`physics`, `fiction`…)

### Шаг 2. Скопировать шаблоны

```bash
cp ~/data/calibre/cwa_doc/docker-compose.yml .
cp -r ~/data/calibre/cwa_doc/custom-cont-init.d .
```

### Шаг 3. Отредактировать `docker-compose.yml`

Заменить:
- `container_name: Books-Topic` → уникальное имя;
- порт `1232:8083` → свободный порт;
- пути `~/data/calibre/books_topic` и `books_topic_dwl` → свои папки
  (папки создадутся автоматически).

### Шаг 4. Первый запуск

```bash
docker compose up -d
```

При первом запуске образ CWA скопирует свой код в `./app` (это нужно, чтобы было
куда класть патчи). Дождаться, пока контейнер станет `healthy`:

```bash
docker ps --filter name=Books-Topic
```

### Шаг 5. Применить патчи (изменённые файлы)

```bash
cd ~/data/.app/calibre/books_topic
cp -r ~/data/calibre/cwa_doc/patches/* ./app/calibre-web-automated/
docker compose restart
```

> Патчи перезаписывают ~24 файла CWA. Список — см. раздел 3.

### Шаг 6. Включить настройки (в UI или напрямую в БД)

В веб-интерфейсе CWA (Settings) либо в БД `config/cwa.db`:

```sql
UPDATE cwa_settings SET auto_metadata_fetch_enabled = 1;  -- мастер-выключатель метаданных
UPDATE cwa_settings SET auto_metadata_ai_enabled = 1;     -- использовать локальный AI
UPDATE cwa_settings SET ai_metadata_url = 'http://172.17.0.1:8899';  -- URL gemma-4
UPDATE cwa_settings SET auto_convert_retained_formats = 'pdf,epub';
```

Либо через UI: отметить **«Enable Automatic Metadata Fetching»** и
**«Use Local AI for Metadata Extraction»**, указать **AI Server URL**.

---

## 5. Как работает импорт (итоговая схема)

1. Файл кладётся в папку импорта (`books_topic_dwl`).
2. Служба `cwa-ingest-service` (inotifywait) замечает **новый** файл.
3. `ingest_processor.py`:
   - если `.djvu` → `ddjvu` конвертирует в PDF;
   - PDF/EPUB/TXT/… → конвертирует в `epub` (target) и сохраняет оригинал `pdf`
     (для DJVU-сканов — только PDF);
   - добавляет epub, затем PDF (retained) **до** метаданных;
   - добавляет книгу в Calibre (`calibredb add`).
4. `metadata_helper.py`:
   - **PDF** → рендер первых 3 страниц → AI определяет `cover_page` → обложка;
   - **epub** → обложка и метаданные из OPF;
   - **fb2** → обложка и метаданные из `<title-info>` (включая аннотацию);
   - AI извлекает title/authors/language/tags/publisher;
   - **аннотация**: из epub/fb2, либо (для PDF) из онлайн-провайдера;
   - пересчитывает `author_sort`;
   - применяет метаданные к книге.

### Особенности inotify

`inotifywait` реагирует только на **новые события** (создание/перемещение), а не
на файлы, уже лежащие в папке. Для «пробуждения» старых файлов используйте:

```bash
~/data/calibre/trigger_ingest.sh
```

(скрипт перебирает файлы в папке импорта и перемещает каждый туда-обратно).

---

## 6. Частые вопросы

**Почему скан-книга (DJVU) сохраняется только в PDF?**
У скана нет текстового слоя; конвертация PDF → epub для сканов в Calibre зависает
и бесполезна. Поэтому для DJVU включён режим «только PDF».

**Почему не определяется автор?**
Обложка (первая страница) часто содержит только название. Авторы обычно на
титульной странице (2–3 стр.) — поэтому проверяются первые 3 страницы.

**Почему в логе `Author 'X' not found to display name in right order`?**
Это рассинхрон между `books.author_sort` и таблицей авторов. В `metadata_helper.py`
добавлен пересчёт `author_sort` при обновлении авторов. Для старых книг рассинхрон
можно вычистить пересчётом `author_sort` из `authors.sort`.

**Извлекается ли аннотация для PDF?**
Да — AI извлекает название/авторов, а аннотацию CWA берёт у онлайн-провайдеров
(если её нет в самом файле). Если аннотации нет ни у одного провайдера, книга
остаётся без описания (без ошибок).

**Что делать, если файл лежит в папке импорта и не обрабатывается?**
Это файл без нового события inotify. Запустите `trigger_ingest.sh`.

**Изменения переживут `docker compose up --force-recreate`?**
- Патчи (`./app`) и `custom-cont-init.d` — да (смонтированы).
- djvulibre ставится при каждом старте скриптом `01-djvu-support.sh`.
# Calibre-web-autamated-local-Ai
