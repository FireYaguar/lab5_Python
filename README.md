# findex

[![CI](https://github.com/OWNER/findex/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/findex/actions/workflows/ci.yml)

Повнотекстовий пошуковик, який будую протягом семестру (курс «Python — Build a Search Engine», 8 лабораторних).
Кожна лабораторна — окремий git-тег (`lab-01`, `lab-02`, …).

## Корпус

**Документація Python**: вихідні тексти `.rst` з репозиторію CPython (`Doc/`): **560 документів, ~15 МБ, 1,8 млн токенів**.
Документацію зручно шукати, і в Лабі 6 краулер додаватиме до цього ж корпусу сторінки docs.python.org (JSON-lines).

Корпус не зберігається в git (`data/` у `.gitignore`). Завантажити його:

```bash
./scripts/get_corpus.sh      # sparse-clone CPython, копіює Doc/**/*.rst у data/python-docs/
```

## Встановлення і запуск

```bash
uv tool install .                         # команда findex у PATH (або: uv tool install <wheel з релізу>)
uv sync                                   # середовище розробки
uv run findex stats data/                 # лінива версія (за замовчуванням)
uv run findex stats data/ --limit 100     # перші 100 документів (itertools.islice)
uv run findex stats data/ --eager         # «жадібна» версія для порівняння
uv run pytest
uv run ruff check .
```

## Структура

```
src/findex/
  corpus.py     # iter_documents(root) -> Iterator[Document]   (.txt/.rst/.md і .jsonl)
  tokenize.py   # tokenize(text) -> Iterator[str], iter_spans(text) -> (token, start, end)
  stats.py      # один прохід: документи, токени, словник, топ-N; час і пік пам'яті
tests/test_tokenize.py
scripts/get_corpus.sh
```

`iter_documents` і `tokenize` — генератори: `next(iter_documents(Path("data")))` повертається одразу, бо читає лише перший файл.
Файли з поганим UTF-8 читаються з `errors="replace"`, а попередження йде в лог (обхід не падає).

## Правила токенізації

1. `unicodedata.normalize("NFC", …)`: `café` і `café` дають один токен.
2. `str.casefold()` замість `lower()`: `Straße` → `strasse`.
3. Токен — це послідовність `\w` (будь-яке письмо, зокрема кирилиця), регулярний вираз `\w+(?:'\w+)*` через `re.finditer`.
4. **Апострофи**: апостроф *усередині* слова зберігається (`don't`, `п'ять`); `’` і `ʼ` перед цим замінюються на `'`. Лапки навколо слова відкидаються.
5. **Дефіси** розділяють слова: `well-known` → `well`, `known`.
6. **Цифри** залишаються токенами: `Python 3.12` → `python`, `3`, `12`.
7. Токени, що складаються лише з `_` (rst-прикраси), відкидаються. Ідентифікатори на кшталт `__init__` і `snake_case` залишаються.

Тести (`tests/test_tokenize.py`): регістр, кирилиця, комбінований наголос, пунктуація й дефіси, апострофи, цифри, підкреслення, порожній рядок, лінивість і JSON-lines для `iter_documents`.

## Вимірювання: жадібна vs лінива версія

Повний корпус (560 документів, 1 809 580 токенів, словник 42 603). Медіана з 3 запусків.
Машина: Intel Xeon 2,1 ГГц, 2 ядра, Python 3.13. Пам'ять заміряна через `tracemalloc` (пік).

| Версія | Документи | Пікова пам'ять | Час |
|---|---|---|---|
| eager (списки) | 560 | 109,3 MiB | 6,99 s |
| lazy (генератори) | 560 | **6,8 MiB** | 6,72 s |
| eager, `--limit 100` | 100 | 13,9 MiB | 0,74 s |
| lazy, `--limit 100` | 100 | 6,8 MiB | 0,72 s |

**Куди пішла пам'ять.** Жадібна версія одночасно тримає в RAM три речі: список усіх 560 текстів (~15 МБ рядків), список зі списками всіх 1,8 млн токенів (кожен елемент — це 8-байтовий покажчик плюс окремий об'єкт `str` приблизно на 50+ байтів) і `Counter`. Більшість зі ~109 MiB займають саме мільйони дрібних рядкових об'єктів, а не сам текст. Лінива версія в кожен момент тримає **один документ** (найбільший файл ≈ 260 КБ), поточний токен і `Counter` — єдиний «стік», який росте законно, бо містить відповідь (42 тис. унікальних термів). Тому її пік не залежить від розміру корпусу: на 100 і на 560 документах він однаковий (6,8 MiB, це переважно `Counter`), а пік жадібної версії росте лінійно. Нулем пік не буває: `Counter` і один документ потрібні завжди. Час майже однаковий, бо основну роботу (regex, NFC, casefold) обидві версії виконують однаково; лінива навіть трохи швидша, бо не алокує списки.

---

# Лабораторна 2 — інвертований індекс

## Що зроблено

```
src/findex/
  index.py    # Posting, DocMeta, індекс, build_index()
  boolean.py  # двовказівникові merge_and/or/not, set-альтернатива (у Лабі 3 винесено з search.py)
  store.py    # save/load: pickle (index.bin) або JSON (index.json) — за розширенням файлу
scripts/lab02_measure.py   # усі таблиці нижче
tests/test_index.py        # побудова, hashable-записи, запити (обидва движки), merge == set, round-trip
```

- `Posting(doc_id, tf, positions=())` і `DocMeta(doc_id, path, title)` — `@dataclass(frozen=True, slots=True)`, тобто їх можна класти в `set`.
- `build_index` робить **один прохід** по лінивому `iter_documents` → `tokenize`. Для поточного документа рахується `Counter`, його вміст скидається в `defaultdict(list)` постінгів, після чого `Counter` викидається. `doc_id` приходять за зростанням, тому списки постінгів відсортовані без окремого сортування. Також зберігаються `doc_lengths` (знадобляться для BM25 у Лабі 3) і `doc_meta`.
- `--positions` додає до постінгів порядкові номери токенів (знадобляться для фраз у Лабі 3).

```bash
uv run findex index data/ --out index.bin [--positions] [--limit N]   # або --out index.json
uv run findex search index.bin "async OR await NOT thread"   # з Лаби 3 — ранжований пошук
uv run python scripts/lab02_measure.py data/
```

Синтаксис запиту: `a b` = AND, `a OR b`, `a NOT b`, `NOT b` (з Лаби 3 — повноцінний парсер, див. нижче).
Обидві команди друкують час і пік пам'яті (`tracemalloc`). Під `tracemalloc` усе виконується в 2–3 рази повільніше, тому в таблицях нижче час заміряно без нього.

Корпус: 560 документів, 42 603 терміни, 331 826 постінгів. Машина та сама, що в Лабі 1.

## merge vs set

Час одного виклику (`timeit.autorange`) на списках `doc_id`:

| Терміни | df | Op | merge (µs) | set (µs) |
|---|---|---|---|---|
| 2 найчастіші: `in`, `the` | 536 / 552 | AND | 48.4 | 18.0 |
| 2 найчастіші: `in`, `the` | 536 / 552 | OR | 51.1 | 16.9 |
| 2 найрідкісніші: `authoring`, `linklint` | 1 / 1 | AND | 0.1 | 0.2 |
| 2 найрідкісніші: `authoring`, `linklint` | 1 / 1 | OR | 0.2 | 0.2 |
| частий + рідкісний: `the`, `authoring` | 552 / 1 | AND | 0.1 | 4.4 |
| частий + рідкісний: `the`, `authoring` | 552 / 1 | OR | 1.6 | 9.4 |

Обидва алгоритми мають складність O(len(a) + len(b)), але різну константу. Кожен крок merge — це байткод Python у циклі `while`. `set(a) & set(b)` виконує ту саму роботу всередині C. Тому на двох **довгих** списках set приблизно у 2,5–3 рази швидший, навіть з урахуванням `sorted()` результату. Merge виграє, коли **один список короткий**: AND зупиняється, щойно короткий список закінчився (0,1 µs), а set однаково хешує всі 552 елементи довгого. На наших 560 документах різниця — мікросекунди. Merge залишаю за замовчуванням, бо він працює потоково, не потребує хешування і переноситься на фразові запити та стиснені постінги (Лаби 3 і 8).

## Збереження: pickle vs JSON

Медіана з 3 запусків:

| Формат | Позиції | Розмір файлу | save (s) | load (s) |
|---|---|---|---|---|
| pickle | ні | 5.6 MiB | 0.80 | 0.94 |
| json | ні | 3.3 MiB | 0.46 | 0.49 |
| pickle | так | 11.1 MiB | 1.11 | 1.24 |
| json | так | 13.0 MiB | 1.91 | 1.00 |

Без позицій JSON вийшов **меншим і швидшим**, бо `store.py` пише його колонками (`{"doc_ids": [...], "tfs": [...]}`) без імені класу на кожному записі. Pickle натомість зберігає 331 тис. окремих об'єктів `Posting`, і кожен frozen+slots dataclass при завантаженні відновлюється через Python-рівневий `__setstate__` (див. таблицю пам'яті: звичайний dataclass вантажиться в 4 рази швидше). З позиціями JSON стає більшим, бо кожне число записується текстом.
**Позиції** збільшують файл у 2 рази (5,6 → 11,1 MiB pickle), а пік пам'яті під час побудови — з 29 до 106 MiB.

**Чому `pickle.load` чужого файлу небезпечний:** потік pickle — це програма для віртуальної машини, яка може наказати завантажувачу викликати будь-яку імпортовану функцію (наприклад, `os.system("...")`) під час відновлення об'єктів. Завантажити pickle з мережі означає виконати чужий код. Тому pickle підходить лише для власного файлу індексу; про це є коментар у `store.py`. Для обміну даними — JSON.

## Пам'ять: три представлення постінгів

Кожне представлення збирається з корпусу під `tracemalloc`. «Retained» — пам'ять, яку займає готовий індекс після побудови (`tracemalloc` current).

| Представлення постінгів | Пік (побудова) | Retained індекс | Файл (pickle) | Load (s) |
|---|---|---|---|---|
| `list[Posting]`, звичайний `@dataclass` | 38.7 MiB | 36.6 MiB | 6.5 MiB | 0.17 |
| `list[Posting]`, `slots=True` | 26.3 MiB | 23.9 MiB | 5.3 MiB | 0.71 |
| пари `array('I')` (doc_ids, tfs) | **17.6 MiB** | **15.1 MiB** | **4.4 MiB** | **0.04** |

**Куди пішли байти.** Спільна частина всіх трьох рядків — словник на 42,6 тис. термінів (самі рядки-ключі плюс хеш-таблиця), це кілька MiB. Решту займають 331 826 постінгів:
- **Звичайний dataclass**: кожен постінг — це об'єкт (~48 байтів) плюс власний `__dict__` для атрибутів (~50+ байтів), плюс 8-байтовий покажчик у списку. Разом ~110 байтів на постінг, приблизно 36 MiB. Малі `int` (< 257) кешовані, тому окремих об'єктів для `tf` майже немає.
- **`slots=True`**: `__dict__` зникає, поля лежать у фіксованих C-слотах прямо в об'єкті (~48 байтів). Разом з покажчиком у списку це ~56 байтів на постінг, тобто приблизно на третину менше.
- **`array('I')`**: об'єктів немає зовсім, лише сирі 4-байтові числа: 8 байтів на постінг (doc_id + tf) плюс невеликий заголовок масиву на кожен термін. Ось чому це найменший і найшвидший для завантаження варіант (pickle просто копіює буфер). У Лабі 8 ці масиви без копіювання стануть NumPy-масивами (`np.frombuffer`).

У поточному індексі використано `slots=True`, як вимагає лаба. `array` залишено для Лаби 8.

---

# Лабораторна 3 — рейтинг і модель об'єктів

## Що зроблено

```
src/findex/
  index.py    # class Index(Mapping[str, list[Posting]]); build_index() @timed
  query.py    # рекурсивний спуск -> дерево Term / Phrase / And / Or / Not (&, |, ~, evaluate)
  scoring.py  # протокол Scorer; TfIdf, BM25(k1=1.5, b=0.75)
  search.py   # search(): match (lru_cache) -> бали -> heapq.nlargest -> SearchResult + сніпет
  store.py    # load() @timed, open_index() — context manager
  timing.py   # @timed з functools.wraps (лог DEBUG)
scripts/lab03_eval.py      # sanity-check і precision@5 (таблиці нижче)
tests/test_lab03.py        # дерева парсера, dunder-и, скорери, кеш, сніпети, open_index при винятку
```

- **`Index`** наслідує `collections.abc.Mapping`: працюють `len(ix)` (кількість термінів), `"python" in ix`, `ix["python"]` (`KeyError`, якщо терміна немає), `for t in ix`, `repr(ix)` → `Index(terms=42_604, docs=560, positions=True)`, а також `keys()/items()/get()` безкоштовно. Крім того є `num_docs`, `avg_doc_length` і `all_doc_ids` (`cached_property`: рахуються один раз, а не для кожного документа), `doc_length()`, `df()`. Для pickle `__getstate__` зберігає лише дані, без кешів.
- **`open_index(path)`** — це `@contextmanager` з `try/finally`, тому `close()` (очищення кешу) виконується навіть при винятку; це перевіряє окремий тест.
- **`Scorer`** — `typing.Protocol` з `__call__(term, posting, index) -> float`. `TfIdf` і `BM25` не мають спільного предка, і `search(..., scorer=...)` приймає будь-який із них.
- **`SearchResult`** — `@dataclass(frozen=True, slots=True, order=True)` з першим полем `score`, тож `sorted(results)` сортує за балом.
- **Парсер**: `python AND (async OR await) NOT java "event loop"` → `And(And(And(Term('python'), Or(Term('async'), Term('await'))), Not(Term('java'))), Phrase(('event', 'loop')))`. Тести порівнюють дерева, наприклад `parse("a OR b c") == Or(Term("a"), And(Term("b"), Term("c")))`. `Phrase` використовує позиції (індекс треба будувати з `--positions`): позиція `p` першого слова плюс `p+1`, `p+2`, … наступних.
- **Сніпети**: вікно ±80 символів навколо місця, де поруч найбільше різних термінів запиту; терміни виділяються як `**term**`. Зсуви беруться з `iter_spans` (regex на NFC-тексті до `casefold`, бо `casefold` може змінити довжину рядка).

```bash
uv run findex index data/ --out index.bin --positions
uv run findex -vv search index.bin 'python AND (async OR await) NOT java "event loop"' -k 3
uv run python scripts/lab03_eval.py index.bin
```

## Сліди @timed і кешу

Той самий запит двічі в одному процесі (лог з CLI Лаби 3; з Лаби 4 кеш перевіряє тест `test_search_results_sorted_and_cached`):

```
DEBUG findex.timing: load took 822.8 ms
DEBUG findex.timing: match took 2.1 ms          <- 1-й раз: розбір і обчислення дерева
DEBUG findex.timing: search took 45.6 ms
DEBUG findex.search: cache hit for 'python AND (async OR await) NOT java "event loop"'
DEBUG findex.timing: search took 43.6 ms        <- 2-й раз: match не виконувався (немає рядка "match took")

 1.  18.53  A conceptual overview of :mod:`!asyncio`  (data/python-docs/howto/a-conceptual-overview-of-asyncio.rst)
    …he **event** **loop**...") **async** def main(): task_b = asyncio.create_task(coro_b()) …
 2.  18.36  Developing with asyncio  (data/python-docs/library/asyncio-dev.rst)
    …the **event** **loop**, no other Tasks can run in the same thread. When a Task executes an ``**await**`` …
```

`@timed` стоїть *під* `@lru_cache` на `match`, тому рядок `match took` з'являється лише при промаху кешу. Основний час пошуку (~43 мс) іде на сніпети: для топ-k документів файл читається з диска і токенізується заново. Перший варіант вибору вікна був квадратичним (79 мс); після переходу на ковзне вікно O(кількість входжень) — 45 мс.

## Три перевірки рейтингу (на корпусі)

**1. Рідкий термін вище частого.** Запит `class OR winsound` (df 380 проти 5): документи, де є лише рідкий `winsound`, стоять вище за всі документи, де є лише частий `class` (BM25 idf: 4,6 проти 0,4).

| rank | BM25 | document | містить |
|---|---|---|---|
| 1 | 10.83 | `library/winsound.rst` | winsound |
| 2 | 8.35 | `library/windows.rst` | winsound |
| 3 | 8.22 | `extending/windows.rst` | winsound |
| 4 | 6.13 | `whatsnew/3.6.rst` | class, winsound |
| 6 | 0.96 | `library/collections.abc.rst` | class (найкращий документ лише з `class`) |

**2. 20-те повторення майже нічого не додає.** Термін `loop`, документ середньої довжини (3 231 токен):

| tf | TF-IDF | BM25 |
|---|---|---|
| 1 | 1.58 | 1.581 |
| 2 | 3.17 | 2.258 |
| 5 | 7.92 | 3.039 |
| 10 | 15.83 | 3.436 |
| 19 | 30.08 | 3.662 |
| 20 | 31.66 | 3.675 |

20-те входження додає до TF-IDF +1,58 (стільки ж, скільки перше), а до BM25 лише +0,013 (0,9 % від першого): це насичення через `k1`.

**3. Короткий документ з одним входженням вище довгого.** Термін `winsound`, в обох документах tf = 1:

| document | довжина | TF-IDF | BM25 |
|---|---|---|---|
| `library/windows.rst` | 26 | 4.72 | 8.35 |
| `whatsnew/3.13.rst` | 15,052 | 4.72 | 1.75 |

TF-IDF не розрізняє ці документи, а BM25 через нормалізацію довжини (`b = 0.75`) дає короткому документу бал у 4,8 раза вищий. При `b = 0` бали були б рівні.

## precision@5: TF-IDF vs BM25

10 запитів, релевантні документи розмічені вручну **до** запуску пошуку (`QRELS` у `scripts/lab03_eval.py`). P@5 = (кількість релевантних у топ-5) / 5. Колонка «max» показує найкраще можливе значення, коли релевантних документів менше п'яти.

| Запит | релевантних | max P@5 | TF-IDF | BM25 |
|---|---|---|---|---|
| `"event loop"` | 5 | 1.0 | 0.6 | 1.0 |
| `"regular expression"` | 2 | 0.4 | 0.4 | 0.4 |
| `unicode normalization` | 2 | 0.4 | 0.4 | 0.4 |
| `"garbage collector"` | 4 | 0.8 | 0.6 | 0.6 |
| `"context manager"` | 3 | 0.6 | 0.2 | 0.2 |
| `thread lock` | 3 | 0.6 | 0.2 | 0.4 |
| `json decode` | 1 | 0.2 | 0.2 | 0.2 |
| `pickle unpickling security` | 4 | 0.8 | 0.2 | 0.2 |
| `decorator wraps` | 1 | 0.2 | 0.2 | 0.2 |
| `"virtual environment"` | 4 | 0.8 | 0.4 | 0.6 |
| **середнє** | | **0.58** | **0.34** | **0.42** |

BM25 виграє (0,42 проти 0,34; стеля 0,58). Різницю видно на `"event loop"` і `thread lock`. TF-IDF лінійний за tf і не штрафує довжину, тому підіймає довгі документи з багатьма згадками: `asyncio-task.rst` і `asyncio-protocol.rst` в одному випадку, `multiprocessing.rst` і `glossary.rst` в іншому. BM25 ставить на їхнє місце короткі профільні сторінки (`asyncio-runner.rst`, `asyncio-dev.rst`, `_thread.rst`). Де обидва програли, це через мене і через AND, а не через формулу:
- `"context manager"`: обидва ставлять першим `contextlib.rst`, далі йдуть модулі, які справді надають context managers (`tempfile`, `multiprocessing`, `decimal`). Моя розмітка (`datamodel`, `compound_stmts`) виявилася занадто вузькою.
- `pickle unpickling security`: неявний AND пропускає лише 4 документи, що містять усі три слова; у `pickletools`, `copyreg`, `shelve` їх немає. Для таких запитів краще підійшов би OR з рейтингом.

Вибірка мала й шумна, але звичка міряти рейтинг закладена; у Лабі 8 ці 10 запитів стануть основою оцінки гібридного пошуку.

---

# Лабораторна 4 — типи, тести, пакування, CLI

## Встановлення і команди

```bash
uv tool install .                                   # з репозиторію
uv tool install findex-0.4.0-py3-none-any.whl       # або з wheel релізу v0.4.0
findex --help
findex index data/ --out index.bin --positions      # смуга прогресу rich (stderr); з Лаби 5: --workers/--executor
findex search index.bin '"event loop" NOT thread' -k 5 [--scorer bm25|tfidf]
findex search index.bin python --json | jq .title   # JSON lines у stdout
findex stats data/ --top 20 [--eager]
findex -v ...  / findex -vv ...                     # INFO / DEBUG логи (до підкоманди)
python -m findex ...                                # те саме без встановлення
```

- **Точка входу**: `[project.scripts] findex = "findex.cli:app"`. Після встановлення wheel у `bin/` з'являється скрипт `findex`, який імпортує `findex.cli` і викликає `app()`.
- **stdout/stderr**: таблиці й `--json` йдуть у stdout; логи (`RichHandler`), смуга прогресу й помилки — у stderr. Логер налаштовується **один раз** у `cli.root` (`-v` → INFO, `-vv` → DEBUG); у бібліотеці лише `logging.getLogger(__name__)`. Діагностичних `print` у бібліотеці не лишилося; `measure`, `@timed` і кеш пишуть у лог.
- **Очікувані помилки** дають один рядок без traceback і ненульовий код виходу:
  ```
  $ findex search nope.bin x          ->  error: index file not found: nope.bin (build it with `findex index`)   exit 1
  $ findex search index.bin '(python' ->  error: bad query: missing ')'                                         exit 2
  ```
- Старі `python -m findex.index/search/stats` (argparse) замінені на підкоманди typer.

## Типи (pyright strict)

`[tool.pyright] typeCheckingMode = "strict"`, перевіряються `src/` і `tests/`: **0 errors**. `scripts/` (разові скрипти вимірювань) у перевірку не входять.
- `Scorer` — `typing.Protocol` (`__call__(term, posting, index) -> float`); `TfIdf` і `BM25` йому відповідають структурно, без наслідування.
- Генератори повертають `Iterator[...]`, функції приймають `Iterable[...]` (`build_index(docs: Iterable[Document])`).
- `Literal`: `type ScorerName = Literal["bm25", "tfidf"]`, `type Engine = Literal["merge", "set"]`. У CLI це `StrEnum`, бо typer будує варіанти вибору з Enum.
- PEP 695: `def timed[**P, R](fn: Callable[P, R]) -> Callable[P, R]`, `def measure[T](...)`.
- `# type: ignore` два, і кожен з поясненням: `__hash__ = None` в `Index` (змінний контейнер з рівністю за значенням) і присвоєння полю frozen-датакласу в тесті (саме його падіння й перевіряється).

## Тести

**63 тести** (pytest), з них 4 властивості Hypothesis; спільні фікстури (крихітний корпус, індекс, файл індексу) лежать у `tests/conftest.py`.

| Файл | Що перевіряє |
|---|---|
| `test_tokenize.py` | parametrize-таблиця: регістр, `ß`, кирилиця, NFC, дефіси, апострофи, цифри, `_`, порожній рядок; зсуви `iter_spans` |
| `test_corpus.py` | лінивість, JSON-lines, зламаний UTF-8, `count_documents` |
| `test_index.py` | постінги й позиції, hashable/frozen, dunder-и `Index`, Boolean-запити, save/load (pickle і JSON), `open_index` при винятку |
| `test_search.py` | дерева парсера, помилки парсера, фрази, обидва скорери, **три sanity-check BM25 з Лаби 3**, кеш, `@timed` + `wraps`, сніпети |
| `test_properties.py` | Hypothesis: токени непорожні й токенізація ідемпотентна; постінги завжди відсортовані; merge == set для AND/OR/NOT; `load(save(ix)) == ix` |
| `test_cli.py` | `CliRunner`: `--help/--version`, index→search, `--json`, stats, помилки з кодами 1/2 без traceback, логи лише в stderr; `slow`: повний корпус |

```bash
uv run pytest                      # швидкі (< 1 с); маркер slow пропущено через addopts
uv run pytest -m slow              # лише повільні (індекс на всьому корпусі, ~25 с)
uv run pytest -m "" --cov          # усе + покриття
```

**Покриття: 97 %** (692 інструкції, 24 не покриті; `pytest -m "" --cov`). Навмисно не покрито:
- `__main__.py` і гілка `PackageNotFoundError` в `__init__.py`: запуск без встановленого пакета, перевіряється вручну;
- `compute_stats_eager` і `measure`-шлях `findex stats --eager`: це навмисно погана версія з Лаби 1, потрібна лише для таблиці;
- гілки помилок, які важко відтворити в тесті: зламаний (не наш) файл індексу, некоректний вузол парсера, порожній корпус (`avg_doc_length` = 0).

## CI і реліз

`.github/workflows/ci.yml` запускається на кожен push і PR: `uv sync --locked` → `ruff check` → `ruff format --check` → `pyright` → `pytest --cov` → `uv build` (wheel як artifact).

Реліз **v0.4.0**: `uv build` → `dist/findex-0.4.0-py3-none-any.whl` прикріплений до GitHub Release. Перевірено встановлення в чисте оточення (окремі `UV_TOOL_DIR`, без репозиторію і `.venv`):
```
$ uv tool install findex-0.4.0-py3-none-any.whl && findex --version
findex 0.4.0
```

---

# Лабораторна 5 — конкурентність і GIL

## Що зроблено

```
src/findex/index.py     # build_partial(paths, start_id) -> PartialIndex;  merge(partials) -> Index
src/findex/parallel.py  # make_chunks(), build_parallel(..., executor=serial|threads|processes)
src/findex/corpus.py    # list_sources(), source_size(): діапазони doc_id відомі наперед
scripts/lab05_run.py    # один вимір у свіжому процесі (wall, CPU, RSS, merge) -> JSON
scripts/lab05_bench.py  # усі конфігурації x 3 прогони -> таблиця + docs/lab05_speedup.png
scripts/lab05_race.py   # гонитва на спільному Counter: GIL vs 3.13t
tests/test_parallel.py  # serial == Lab 4, три виконавці дають однаковий індекс, падіння воркера
```

```bash
findex index data/ --out index.bin                                   # типово: processes, workers = cpu_count
findex -v index data/ --out index.bin --executor threads --workers 4
uv run python scripts/lab05_bench.py data/ --ft .venv-ft/bin/python  # бенчмарк (див. нижче про 3.13t)
```

- **Map-reduce.** `build_partial(paths, start_id)` — функція рівня модуля, тож процес може її отримати через pickle. У воркер передаються **шляхи**, а не прочитані документи: кожен воркер сам читає свої файли. `merge` склеює постінги за термінами в порядку `doc_id`.
- **Унікальні `doc_id`.** Кожне джерело заздалегідь резервує діапазон: текстовий файл — 1 id, `.jsonl` — по одному на рядок (`source_size`). `make_chunks` ділить відсортований список файлів на шматки приблизно однакового **розміру в байтах** і дає кожному свій `start_id` (префіксна сума). Тому `doc_id` не залежать від того, як корпус поділено на шматки.
- **Однаковий результат.** `merge([build_partial(усі шляхи, 0)]) == build_index(iter_documents(...))` перевіряє окремий тест. Ще один тест (`slow`) на повному корпусі показує, що `processes` дає той самий індекс; файли pickle для serial, threads і processes збігаються **байт-у-байт** (`cmp`).
- **Безпека для spawn.** `ProcessPoolExecutor(mp_context=get_context("spawn"))` запускає чистий інтерпретатор на воркер. `python -m findex` тепер під `if __name__ == "__main__"`, бо під spawn дочірній процес заново імпортує `__main__`; console-script `findex` має такий захист сам.
- **Падіння воркера.** `fut.result()` перекидає виняток у батьківський процес разом зі стеком воркера (`__cause__`, `_RemoteTraceback`). Решта шматків скасовується (`shutdown(cancel_futures=True)`), тож нічого не зависає. Це перевіряє тест `test_worker_error_propagates`.
- **Податок pickle, виміряний і зменшений.** Спочатку воркер повертав об'єкти `Posting`, і батьківський процес послідовно розпаковував 331 тис. frozen-slots датакласів **0,84 с**; процеси були повільніші за serial. Тепер воркер повертає прості кортежі `(doc_id, tf, positions)`: pickle займає 0,05 с, розмір — 2,7 MiB замість 5,6. Об'єкти `Posting` створює `merge`, і це послідовна частина збірки.

## Машина

**Intel Xeon @ 2,1 ГГц, 2 ядра (1 потік на ядро), `os.cpu_count() = 2`**, Linux, CPython 3.13.18; free-threaded збірка — CPython 3.13.14t. Корпус: 560 файлів, 15 МБ, 1,8 млн токенів.

## Бенчмарк

Кожна клітинка — медіана з 3 прогонів, кожен прогін у **свіжому процесі** (`lab05_run.py`), щоб пік RSS батьківського процесу (`ru_maxrss`) належав саме цьому прогону. CPU = user + sys батьківського процесу **і** завершених воркерів (`os.times()`). «±» — розкид (max − min) wall за трьома прогонами. Прискорення рахується відносно serial на звичайній збірці з GIL. Набір воркерів {1, 2, 4, 8, cpu_count}, а cpu_count тут 2.

| Python | Executor | Workers | Wall (s, median) | ± | CPU (s) | Peak RSS parent (MiB) | Merge (s) | Speedup |
|---|---|---|---|---|---|---|---|---|
| 3.13 (GIL on) | serial | 1 | 1.78 | 0.11 | 1.75 | 81 | 0.23 | 1.00× |
| 3.13 (GIL on) | threads | 1 | 1.70 | 0.28 | 1.68 | 88 | 0.27 | 1.04× |
| 3.13 (GIL on) | threads | 2 | 1.74 | 0.12 | 1.74 | 92 | 0.30 | 1.02× |
| 3.13 (GIL on) | threads | 4 | 1.73 | 0.14 | 1.73 | 101 | 0.27 | 1.03× |
| 3.13 (GIL on) | threads | 8 | 1.81 | 0.06 | 1.82 | 109 | 0.28 | 0.99× |
| 3.13 (GIL on) | processes | 1 | 1.76 | 0.10 | 1.80 | 96 | 0.20 | 1.01× |
| 3.13 (GIL on) | processes | 2 | **1.20** | 0.09 | 1.89 | 98 | 0.20 | **1.48×** |
| 3.13 (GIL on) | processes | 4 | 1.30 | 0.25 | 2.21 | 102 | 0.23 | 1.37× |
| 3.13 (GIL on) | processes | 8 | 1.53 | 0.06 | 2.66 | 107 | 0.25 | 1.17× |
| 3.13t (GIL off) | serial | 1 | 2.22 | 0.14 | 2.21 | 88 | 0.34 | 0.80× |
| 3.13t (GIL off) | threads | 1 | 2.31 | 0.49 | 2.31 | 94 | 0.36 | 0.77× |
| 3.13t (GIL off) | threads | 2 | 1.86 | 0.09 | 2.78 | 98 | 0.42 | 0.96× |
| 3.13t (GIL off) | threads | 4 | 1.86 | 0.13 | 2.78 | 114 | 0.45 | 0.96× |
| 3.13t (GIL off) | threads | 8 | 1.90 | 0.19 | 2.86 | 127 | 0.47 | 0.94× |

![speedup](docs/lab05_speedup.png)

`sys._is_gil_enabled()` у прогонах на 3.13t повертає `False` (колонка «GIL off» береться саме з нього; `lab05_run.py` записує це значення в кожен рядок JSON).

Оскільки процеси виграють, `findex index` типово запускається з `--executor processes --workers <cpu_count>`.

## Гонитва на 3.13t (`scripts/lab05_race.py`)

Наш `build_partial` нічого спільного між потоками не змінює: кожен шматок має власні словники, а батьківський процес лише склеює результати. Тому в самому індексаторі гонитви немає. Щоб побачити, що саме приховував GIL, скрипт робить «наївну» паралельну статистику: 4 потоки рахують токени в **один спільний `Counter`** через `shared[tok] += 1`.

```
Python 3.13.18, GIL enabled: True
naive shared Counter #1: 1,809,580 / 1,809,580 (lost 0), 0.31 s
fix 1, Lock around +=:       1,809,580 (lost 0), 0.57 s
fix 2, per-thread Counter:   1,809,580 (lost 0), 0.21 s
---
Python 3.13.14, GIL enabled: False
naive shared Counter #1: 1,796,859 / 1,809,580 (lost 12,721), 0.71 s
naive shared Counter #2: 1,795,395 / 1,809,580 (lost 14,185), 0.68 s
naive shared Counter #3: 1,794,863 / 1,809,580 (lost 14,717), 0.73 s
fix 1, Lock around +=:       1,809,580 (lost 0), 1.60 s
fix 2, per-thread Counter:   1,809,580 (lost 0), 0.13 s
```

`shared[tok] += 1` — це три кроки: прочитати значення, додати 1, записати. Якщо два потоки одночасно прочитали те саме значення, один із записів губиться. Під GIL такий збіг майже неможливий, бо потік зазвичай не перемикається посеред цих кроків, і на трьох прогонах втрат 0. Але код від цього не стає коректним: GIL захищає внутрішній стан інтерпретатора, а не наш інваріант «сума = кількість токенів». Без GIL потоки справді виконуються одночасно, і губиться ~13–15 тис. оновлень (0,8 %) щоразу по-різному.

Виправлення: (1) `Lock` навколо `+=` — коректно, але на 3.13t у 2,3 раза повільніше за наївний варіант, бо потоки стоять у черзі за замком; (2) **прибрати спільний стан**: у кожного потоку свій `Counter`, а в кінці вони зливаються. Це коректно і найшвидше; саме так працюють `build_partial` і `merge`.

## Чому так вийшло (своїми словами)

**Потоки під GIL не прискорили індексацію.** Індексація — це робота Python-коду на процесорі: regex, `casefold`, оновлення словників. Щоб виконувати байткод, потік мусить тримати GIL, а тримати його може лише один потік. Тому 2, 4 чи 8 потоків по черзі передають один одного GIL і виконують ту саму роботу на одному ядрі: wall ≈ CPU ≈ serial (1,7–1,8 с), а з 8 потоками трохи гірше через перемикання. Потоки допомагають там, де під час очікування GIL відпускається: читання з диска, мережа, `time.sleep` (Лаба 6).

**Процеси прискорили, але не вдвічі, і за це заплачено.** У кожного процесу свій інтерпретатор і свій GIL, тож на 2 ядрах два воркери справді працюють одночасно: wall 1,78 → 1,20 с (1,48×). Чим заплачено:
- **CPU time > wall**: 1,89 с CPU за 1,20 с wall. Додатковий час іде на запуск інтерпретаторів через spawn (кожен заново імпортує `findex`), на pickle аргументів і результатів, на розпаковування в батьківському процесі.
- **Більше воркерів, ніж ядер, — гірше**: 4 і 8 процесів на 2 ядрах не мають додаткових ядер, а запуск і pickle кожного процесу коштує. CPU росте до 2,66 с, а wall погіршується до 1,53 с.
- **RSS**: пік батьківського процесу 81 → 98–107 MiB, бо він тримає розпаковані часткові індекси й готовий індекс водночас. До того ж кожен воркер має власну копію інтерпретатора і своїх даних; цю пам'ять колонка RSS батьківського процесу не показує, але вона теж витрачена.

**Закон Амдала в merge.** `merge` послідовний: 0,20–0,25 с із ~1,78 с, тобто ~13 % (сюди ж входить створення 331 тис. об'єктів `Posting`). Навіть з нескінченною кількістю ядер прискорення не перевищить 1 / 0,13 ≈ **7,7×**. Реальна стеля нижча: сюди ж додаються послідовний `list_sources`, запуск пулу і розпаковування результатів у батьківському процесі. На 2 ядрах ідеал — 2×, отримали 1,48×; різницю з'їли ці послідовні частини.

**Що змінилось на 3.13t.** `sys._is_gil_enabled() == False`, і потоки справді працюють паралельно: CPU 2,78 с за wall 1,86 с, тобто два ядра зайняті одночасно (під GIL CPU завжди ≈ wall). Відносно власного serial на 3.13t потоки дали 2,22 → 1,86 с (1,19×). Проте сама free-threaded збірка поки що повільніша в одному потоці (~25 % на serial: 2,22 проти 1,78 с), бо атомарні лічильники посилань і блокування на рівні об'єктів мають свою ціну. Ще частину виграшу з'їдає конкуренція потоків за спільні об'єкти (регулярний вираз, інтерновані рядки, алокатор). Тому на нашій машині з 2 ядрами потоки 3.13t так і не обігнали serial зі звичайним GIL, а процеси лишились найкращим варіантом. Зате без GIL з'явилась справжня гонитва (див. вище): код, який роками «працював» під GIL, на 3.13t рахує неправильно.

**Протилежний дослід** (ввід-вивід, де потоки масштабуються) я не робив: він необов'язковий, а корпус на 15 МБ повністю лежить у кеші ОС, тож читання файлів тут не впирається в диск. Мережевий варіант того самого буде в Лабі 6 (async-краулер).

### Як відтворити 3.13t

```bash
uv python install 3.13t
uv venv .venv-ft -p 3.13t && uv pip install --python .venv-ft/bin/python -e .
.venv-ft/bin/python -c "import sys; print(sys._is_gil_enabled())"   # False
.venv-ft/bin/python scripts/lab05_race.py data/
```
