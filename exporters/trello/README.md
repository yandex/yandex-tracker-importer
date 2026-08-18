# Экспортер из Trello в формат для универсального импортера в Yandex Tracker

Документация по списку экспортируемых сущностях описана в [`docs/index.md`](docs/index.md).

> **ИИ-агентам:** пошаговый runbook по проведению миграции — в [`AGENTS.md`](AGENTS.md).

## Быстрый старт

### 1. Соберите Docker/Podman образ

**Docker:**

```bash
docker build -t trello-exporter:latest .
```

**Podman:**

```bash
podman build -t trello-exporter:latest .
```

### 2. Подготовьте рабочую директорию

Список файлов конфигурации и как их получить описан в [`docs/index.md`](docs/index.md).

Рабочая директория должна содержать:

```
configs/boards.cfg  
configs/source_trello_api.cfg  
configs/source_trello_token.cfg
logs/     ← будет создан автоматически, если его нет
logs/     ← будет создан автоматически, если его нет и содержать результаты экспорта
```

### 3. Запустите экспортер

Экспортер работает в контейнере и использует примонтированную рабочую директорию `/work`. Далее приведён пример команды запуска из каталога, где находится папка configs с настройками подключения к trello.

**Docker:**

```bash
docker run --rm \
  -v "$(pwd)":/work \
  -w /work \
  trello-exporter:latest
```

**Podman:**

```bash
podman run --rm \
  --userns=keep-id \
  -v "$(pwd)":/work \
  -w /work \
  trello-exporter:latest
```

После выполнения логи появятся в директории `logs` с логами запуска и `data` с файлами экспортнутых сущностей из trello в формате, пригодном для импорта в Yandex Tracker.

## Документация

Дополнительная документация доступна в [`docs/`](docs/).

## Требования

* Доступ к API trello
* Установленный Docker или Podman
