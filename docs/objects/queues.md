# Очереди

**Файл для импорта:** `queue.yaml`  
**Расположение:** `./data/queues/<queue_key>/queue.yaml`

> Примечание: Поля, отмеченные звёздочкой (<span style="color: red;">*</span>), являются обязательными.

## Общие сведения

| Поле                                                 | Тип             | Описание                              |
|------------------------------------------------------|-----------------|---------------------------------------|
| `key`<span style="color: red;">*</span>              | Строка          | Ключ очереди                          |
| `name`<span style="color: red;">*</span>             | Строка          | Название очереди                      |
| `lead`<span style="color: red;">*</span>             | Строка          | Владелец очереди (логин)              |
| `defaultType`<span style="color: red;">*</span>      | Строка          | Тип задачи по умолчанию               |
| `defaultPriority`<span style="color: red;">*</span>  | Строка          | Приоритет задачи по умолчанию         |
| `issueTypesConfig`<span style="color: red;">*</span> | Массив объектов | Конфигурация типов задач очереди      |
| `workflows`<span style="color: red;">*</span>        | Массив объектов | Рабочие процессы очереди              |
| `description`                                        | Строка          | Описание очереди                      |
| `teamUsers`                                          | Массив          | Участники команды (массив логинов)    |
| `assignAuto`                                         | Логическое      | Автоматическое назначение исполнителя |
| `denyVoting`                                         | Логическое      | Запрет на голосование за задачи       |
| `components`                                         | Массив объектов | Компоненты очереди                    |
| `versions`                                           | Массив объектов | Версии очереди                        |
| `macros`                                             | Массив объектов | Макросы очереди                       |
| `triggers`                                           | Массив объектов | Триггеры очереди                      |
| `autoactions`                                        | Массив объектов | Автодействия очереди                  |
| `local_fields`                                       | Массив объектов | Локальный поля очереди                |
| `permissions`                                        | Объект          | Права доступа                         |

---

## Структура `issueTypesConfig`

```yaml
- issueType: milestone
  resolutions:
    - fixed
    - newresolution
  workflow: W1
```

| Поле                                          | Тип    | Описание                         |
|-----------------------------------------------|--------|----------------------------------|
| `issueType`<span style="color: red;">*</span> | Строка | Ключ типа задачи                 |
| `workflow`<span style="color: red;">*</span>  | Строка | Идентификатор рабочего процесса  |
| `resolutions`                                 | Массив | Список резолюций для типа задачи |

---

## Структура `workflows`

```yaml
- id: W1
  name: Рабочий процесс
  initialAction:
    name:
      en: Undefined
      ru: Не задано
    target: new
  steps:
    - status: new
      actions:
        - name:
            en: Back to work
            ru: В работу
          target: inProgress
      metaAction:
        name:
          en: New
          ru: Новый
        target: new
```

| Поле                                              | Тип    | Описание                                                                                       |
|---------------------------------------------------|--------|------------------------------------------------------------------------------------------------|
| `id`<span style="color: red;">*</span>            | Строка | Идентификатор рабочего процесса                                                                |
| `name`<span style="color: red;">*</span>          | Строка | Название рабочего процесса                                                                     |
| `initialAction`<span style="color: red;">*</span> | Объект | Начальное действие рабочего процесса                                                           |
| `steps`<span style="color: red;">*</span>         | Массив | Шаги рабочего процесса (если в статус можно попасть из любого статуса, используйте metaAction) |

---

## Структура `components`

```yaml
- name: Component Name
  description: Component description
  assignAuto: false
  lead: component_lead_username
```

| Поле                                            | Тип        | Описание                           |
|-------------------------------------------------|------------|------------------------------------|
| `name`<span style="color: red;">*</span>        | Строка     | Название компонента                |
| `description`<span style="color: red;">*</span> | Строка     | Описание компонента                |
| `assignAuto`                                    | Логическое | Признак автоматического назначения |
| `lead`                                          | Строка     | Владелец компонента                |

---

## Структура `versions`

```yaml
- name: Version Name
  description: Version description
  startDate: '2020-05-10'
  dueDate: '2030-10-15'
```

| Поле                                     | Тип     | Описание                                     |
|------------------------------------------|---------|----------------------------------------------|
| `name`<span style="color: red;">*</span> | Строка  | Название версии                              |
| `description`                            | Строка  | Описание версии                              |
| `startDate`                              | Строка  | Дата начала версии (формат `YYYY-MM-DD`)     |
| `dueDate`                                | Строка  | Дата завершения версии (формат `YYYY-MM-DD`) |

---

## Структура `macros`

```yaml
- name: Macros name
  body: message text
  issueUpdate:
    description: New issue
    tags:
      add: "New tag"
```

| Поле                                     | Тип     | Описание                                                |
|------------------------------------------|---------|---------------------------------------------------------|
| `name`<span style="color: red;">*</span> | Строка  | Название макроса                                        |
| `body`                                   | Строка  | Сообщение, которое будет создано при выполнении макроса |
| `issueUpdate`                            | Объект  | Объект со списком полей задачи, которые требуется изменить. Чтобы очистить поле, укажите значение `null`. Также допустимо использовать операторы `set`, `add`, `remove` (см. [Редактирование параметров](https://yandex.ru/support/tracker/ru/common-format#edit-fields)) |

---

## Структура `triggers`

```yaml
- name: Trigger name
  actions:
    - type: Transition
      status:
        key: open
  conditions:
    - type: CommentFullyMatchCondition
      word: Open
```

| Поле                                        | Тип             | Описание                                                            |
|---------------------------------------------|-----------------|---------------------------------------------------------------------|
| `name`<span style="color: red;">*</span>    | Строка          | Название триггера                                                   |
| `actions`<span style="color: red;">*</span> | Массив объектов | Массив с действиями триггера. Подробнее в разделе [Объекты действий триггера](https://yandex.ru/support/tracker/ru/concepts/queues/change-trigger-actions) |
| `conditions`                                | Массив объектов | Массив с условиями срабатывания триггера. Подробнее в разделе [Объекты условий срабатывания триггера](https://yandex.ru/support/tracker/ru/concepts/queues/change-trigger-conditions) |
| `active`                                    | Логический      | Статус триггера. <br>`true` - активный; <br>`false` - неактивный    |

---

## Структура `autoactions`

```yaml
- name: AutoactionName
  filter:
    priority:
      - critical
    status:
      - inProgress
  actions:
    - type: Transition
      status:
        key: needInfo
```

| Поле                                        | Тип             | Описание                                                                          |
|---------------------------------------------|-----------------|-----------------------------------------------------------------------------------|
| `name`<span style="color: red;">*</span>    | Строка          | Название автодействия                                                             |
| `filter`<span style="color: red;">*</span>  | Массив объектов | Массив с условиями фильтрации полей задач, для которых сработает автодействие     |
| `actions`<span style="color: red;">*</span> | Массив объектов | Массив с действиями над задачами                                                  |
| `active`                                    | Логический      | Статус автодействия. <br>`true` - активный; <br>`false` - неактивный              |
| `enableNotifications`                       | Логический      | Статус отправки уведомлений. <br>`true` - отправлять; <br>`false` - не отправлять |
| `intervalMillis`                            | Число           | Периодичность запуска автодействия в миллисекундах. По умолчанию выставляется значение `3600000` (1 раз в час) |

---

## Структура `local_fields`

```yaml
- category:
    en: System
    ru: Системные
  description: Have access to
  key: haveAccess
  name:
    en: Have access
    ru: Имеют доступ
  schema:
    type: array
    items: user
  optionsProvider:
    type: FixedUserListOptionsProvider
```

| Поле                                         | Тип        | Описание                                 |
|----------------------------------------------|------------|------------------------------------------|
| `key`<span style="color: red;">*</span>      | Строка     | Уникальный ключ поля                     |
| `name`<span style="color: red;">*</span>     | Объект     | Название поля на двух языках             |
| `category`<span style="color: red;">*</span> | Строка     | Категория поля на русском языке          |
| `schema`<span style="color: red;">*</span>   | Объект     | Тип поля                                 |
| `description`                                | Строка     | Описание поля                            |
| `optionsProvider`                            | Объект     | Объект с информацией об элементах списка |

---

## Структура `schema`

```yaml
type: array
items: user
```

| Поле                                     | Тип     | Описание                                                                                               |
|------------------------------------------|---------|--------------------------------------------------------------------------------------------------------|
| `type`<span style="color: red;">*</span> | Строка  | Тип поля. В случае если поле является списком со множественным выбором, нужно указать значение `array`. Поля могут быть одним из следующих типов: <br>`date` — Дата; <br>`datetime` — Дата/Время; <br>`string` — Текстовое однострочное поле; <br>`text` — Текстовое многострочное поле; <br>`float` — Дробное число; <br>`integer` — Целое число; <br>`user` — Имя пользователя; <br>`uri` — Ссылка. |
| `items`                                  | Строка  | В случае, если поле является списком со множественным выбором, указывается тип данных элементов списка. В иных случаях - не указывается |

---

## Структура `optionsProvider`

| Поле                                     | Тип     | Описание                                                                                                   |
|------------------------------------------|---------|------------------------------------------------------------------------------------------------------------|
| `type`<span style="color: red;">*</span> | Строка  | Тип выпадающего списка. Выпаюащий список может быть одним из следующих типов: <br>`FixedListOptionsProvider` — Список строковых или числовых значений; <br>- `FixedUserListOptionsProvider` — Список пользователей. |
| `values`                                 | Массив  | Массив со значениями выпадающего списка (указывается только в случае, если тип `FixedListOptionsProvider`) |

---

### Структура `permissions`

| Поле     | Тип      | Описание                                     |
|----------|----------|----------------------------------------------|
| `create` | Объект   | Разрешения на создание задач в очереди       |
| `grant`  | Объект   | Разрешения на редактирование задач в очереди |
| `read`   | Объект   | Разрешения на чтение задач в очереди         |
| `write`  | Объект   | Разрешения на изменение настроек очереди     |

Каждый из объектов содержит перечень пользователей, групп, ролей, к которым применяется действие разрешения. Укажите в перечне хотя бы одно из полей:

| Параметр   | Описание              |
|------------|-----------------------|
| `users`    | Список пользователей  |
| `groups`   | Список групп          |
| `roles`    | Список ролей          |

---

## Пример `queue.yaml`

```yaml
key: QUEUEKEY
name: Queue Name
lead: lead_username
defaultType: task
defaultPriority: normal
issueTypesConfig:
  - issueType: milestone
    resolutions:
      - fixed
      - newresolution
    workflow: W1
description: Queue description
teamUsers:
  - Username_1
  - Username_2
assignAuto: true
denyVoting: false
workflows:
  - id: W1
    name: New Workflow
    initialAction:
      name:
        en: Undefined
        ru: Не задано
      target: new
    steps:
      - status: new
        actions:
          - name:
              en: Back to work
              ru: В работу
            target: inProgress
components:
  - name: Component Name
    description: Component description
    assignAuto: false
    lead: component_lead_username
versions:
  - name: Version Name
    description: Version description
    startDate: '2020-05-10'
    dueDate: '2030-10-15'
local_fields:
  - category: Системные
    key: note
    name:
      en: Note
      ru: Примечание
    schema:
      type: string
    description: Note to issue
permissions:
  create:
    users:
    - username
  grant:
    groups:
    - Все сотрудники
    roles:
    - queue-lead
  read:
    roles:
    - access
    - follower
  write:
    roles:
    - queue-lead
    users:
    - username
```

---
