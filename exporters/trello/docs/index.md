# Trello Exporter

## Экспортируемые объекты

* [Карточки](./objects/cards.md)
* [Доски](./objects/boards.md)

## Общая информация

Инструмент позволяет экспортировать данные из Trello в формате YAML, совместимом с универсальным импортёром в Yandex Tracker.
Подробные описания форматов файлов можно найти в документации импортёра.

## Использование и конфигурация

Подробные инструкции по получению конфигурационных файлов, включая Trello API key и OAuth токен, см. в [`docs/index.md`](docs/index.md).

Для запуска экспорта необходимо заполнить конфигурационные файлы и запустить главный исполняемый файл (`app.py`).
Файлы конфигурации находятся в директории `config/`.
При заполнении файла `boards.cfg` необходимо помимо ID доски указать ключ очереди в Yandex Tracker, в которую будут импортироваться данные.

## Получение Trello API key и OAuth токена

Для работы экспортера необходимы **API key** и **OAuth token** Trello.

### Шаг 1. Получить API key

1. Открой страницу:

   ```
   https://trello.com/app-key
   ```
2. Авторизуйтесь в Trello, если потребуется.
3. Скопируйте значение **Key** — это ваш Trello API key.
4. Вставьте его в файл `config/source_trello_api.cfg`.

Пример содержимого файла:

```
xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Шаг 2. Получить OAuth token

1. На той же странице (`https://trello.com/app-key`) нажмите ссылку **Token**
   или откройте URL вида:

   ```
   https://trello.com/1/authorize?expiration=never&name=TrelloExporter&scope=read&response_type=token&key=<YOUR_API_KEY>
   ```
2. Подтвердите доступ.
3. Скопируйте сгенерированный токен.
4. Вставьте его в файл `config/source_trello_token.cfg`.

Пример содержимого файла:

```
ATTAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Права доступа

Экспортеру достаточно следующих прав:

* `read` — чтение досок, карточек, комментариев и вложений

Рекомендуется выпускать токен с параметром `expiration=never`, если экспортер используется регулярно.

## Конфигурационные файлы

| Файл                    | Описание                      | Пример содержимого                                                                                   |
| ----------------------- | ----------------------------- | ---------------------------------------------------------------------------------------------------- |
| boards.cfg              | Список ID досок для экспорта. | <pre><code>FwNdCIsu:FIRSTQUEUEKEY<br>WGd6ztj3:SECONDQUEUEKEY</code></pre>                            |
| source_trello_api.cfg   | API ключ Trello.              | <pre><code>xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code></pre>                                            |
| source_trello_token.cfg | OAuth токен Trello.           | <pre><code>ATTAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code></pre> |

## Структура файлов после экспорта

```
data/
├── queues/
│   └── <queue_key>/
│       ├── queue.yaml
│       └── <issue_key>/
│           ├── issue.yaml
│           └── attachments/
│               ├── file_123.pdf
│               └── image_321.png
├── projects/
    └── <project_name>/
        └── project.yaml
```

## Требования

* Учетная запись Trello с доступом к экспортируемым доскам
* Доступ к API Trello (API key и OAuth token)
* Установленный Docker или Podman

## Особенности работы

1. При экспорте отдельной доски из файла `boards.cfg` вместе с ней автоматически экспортируются все карточки, которые находятся на ней.
2. В полях, где требуется пользователь, устанавливается полное имя данного пользователя, при импорте в Yandex Tracker его следует заменить на логин.
