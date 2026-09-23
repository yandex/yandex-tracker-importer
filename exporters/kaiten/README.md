# Экспортер из Kaiten в формат для универсального импортера в Yandex Tracker

Инструмент выгружает доски и карточки Kaiten в YAML-формат, совместимый с
универсальным импортером в Yandex Tracker. Описание форматов выходных файлов
см. в документации импортёра.

> **ИИ-агентам:** пошаговый runbook по проведению миграции — в [`AGENTS.md`](AGENTS.md).

## Быстрый старт

### 1. Соберите Docker/Podman образ

**Docker:**

```bash
docker build -t kaiten-exporter:latest .
```

**Podman:**

```bash
podman build -t kaiten-exporter:latest .
```

### 2. Подготовьте рабочую директорию

Рабочая директория должна содержать каталог `configs/` с настройками подключения:

```
configs/
├── source_kaiten_url.cfg     # адрес инстанса Kaiten, напр. https://acme.kaiten.ru
├── source_kaiten_token.cfg   # API-токен Kaiten (Bearer)
└── boards.cfg                # <board_id>:<QUEUE_KEY> по строке на доску
```

Каталоги `data/` (результат) и `logs/` создаются автоматически.

### 3. Запустите экспортер

Экспортер работает в контейнере и использует примонтированную рабочую директорию
`/work`. Команда запускается из каталога, где лежит папка `configs`.

**Docker:**

```bash
docker run --rm \
  -v "$(pwd)":/work \
  -w /work \
  kaiten-exporter:latest
```

**Podman:**

```bash
podman run --rm \
  --userns=keep-id \
  -v "$(pwd)":/work \
  -w /work \
  kaiten-exporter:latest
```

После выполнения результат появится в `data/`, логи — в `logs/`.

> Внутри Аркадии экспортер также собирается через `ya make` и запускается бинарём
> `./kaiten` из каталога с `configs/`.

## Получение API-токена Kaiten

1. Откройте профиль в Kaiten → **Настройки профиля** → **API-ключ**.
2. Сгенерируйте токен и вставьте его в `configs/source_kaiten_token.cfg`.
3. В `configs/source_kaiten_url.cfg` укажите адрес вашего инстанса
   (`https://<домен>.kaiten.ru`).

Токену достаточно прав на чтение досок, карточек, комментариев и вложений.

## Конфигурационные файлы

| Файл                     | Описание                                | Пример                                        |
| ------------------------ | --------------------------------------- | --------------------------------------------- |
| `boards.cfg`             | Доски для экспорта и ключи очередей.     | `1234:FIRSTQUEUE`<br>`5678:SECONDQUEUE`       |
| `source_kaiten_url.cfg`  | Адрес инстанса Kaiten.                   | `https://acme.kaiten.ru`                      |
| `source_kaiten_token.cfg`| API-токен Kaiten.                        | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`        |

## Соответствие сущностей

| Kaiten                        | Yandex Tracker                                   |
| ----------------------------- | ------------------------------------------------ |
| Доска (board)                 | Очередь (`queue.yaml`) + проект (`project.yaml`) |
| Колонка (column)              | Статус (`statuses.yaml`, шаги воркфлоу)          |
| Карточка (card)               | Задача (`issue.yaml`)                            |
| Тип карточки (card type)      | Тип задачи (`issue_types.yaml`)                  |
| Ответственный / участники     | `assignee` / `followers`                         |
| Комментарии                   | `comments`                                       |
| Чек-листы                     | `checklistItems` (объединяются в один список)    |
| Файлы                         | `attachments` + файлы в `attachments/`           |
| Списания времени (time logs)  | `worklogs`                                        |
| Метки (tags)                  | `tags`                                           |
| Кастомные свойства            | Локальные поля очереди + `local_*` у задач       |

## Структура файлов после экспорта

```
data/
├── queues/
│   └── <QUEUE_KEY>/
│       ├── queue.yaml
│       └── <QUEUE_KEY>-<N>/
│           ├── issue.yaml
│           └── attachments/
│               ├── spec_55.pdf
│               └── image_56.png
├── projects/
│   └── <board_title>/
│       └── project.yaml
├── statuses.yaml
├── issue_types.yaml
└── categories.yaml
```

## Особенности работы

1. Пользователи выгружаются по e-mail. При импорте в Yandex Tracker их нужно
   сопоставить с логинами через `mapping/users.cfg`.
2. Каждая доска из `boards.cfg` экспортируется вместе со всеми её карточками.
3. Ключи задач формируются как `<QUEUE_KEY>-<N>`, где `N` — порядковый номер
   карточки по дате создания.
