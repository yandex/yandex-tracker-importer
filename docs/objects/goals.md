# Цели

**Файл для импорта:** `goal.yaml`  
**Расположение:** `./data/goals/<goal_summary>/goal.yaml`
(В качестве `<goal_summary>` может быть любое название)

> Примечание: Поля, отмеченные звёздочкой (<span style="color: red;">*</span>), являются обязательными.

---

## Общие сведения

| Поле                                        | Тип             | Описание                                                                              |
|---------------------------------------------|-----------------|---------------------------------------------------------------------------------------|
| `summary`<span style="color: red;">*</span> | Строка          | Название                                                                              |
| `id`<span style="color: red;">*</span>      | Строка          | Уникальный идентфикатор (можно указать абсолютно любую строку, главное - чтобы каждой сущности соответствовал один идентификатор) |
| `teamAccess`                                | Логический      | Доступ команды                                                                        |
| `description`                               | Строка          | Описание цели                                                                         |
| `markupType`                                | Строка          | Тип отображаемой в тексте разметки                                                    |
| `author`                                    | Строка          | Автор (логин)                                                                         |
| `lead`                                      | Строка          | Ответственный (логин)                                                                 |
| `teamUsers`                                 | Массив строк    | Участники (массив логинов)                                                            |
| `clients`                                   | Массив строк    | Заказчики (массив логинов)                                                            |
| `followers`                                 | Массив          | Наблюдатели (массив логинов)                                                          |
| `start`                                     | Дата            | Дата начала в формате `YYYY-MM-DDThh:mm:ss.sss±hhmm`                                  |
| `end`                                       | Дата            | Дедлайн в формате `YYYY-MM-DDThh:mm:ss.sss±hhmm`                                      |
| `tags`                                      | Массив строк    | Теги                                                                                  |
| `entityStatus`                              | Строка          | Статус: <br>`draft` - Новая; <br>`according_to_plan` - По плану; <br>`at_risk` - Есть риски; <br>`blocked` - Заблокирована;<br>`achieved` - Достигнута; <br>`partially_achieved` - Частично достигнута; <br>`not_achieved` - Не достигнута; <br>`exceeded` - Превышена; <br>`cancelled` - Отменена. |
| `parentEntity`                              | Строка          | Идентификатор (`id`) родительской сущности                                            |
| `checklistItems`                            | Массив объектов | Чеклисты                                                                              |
| `links`                                     | Массив объектов | Связи между сущностями                                                                |
| `comments`                                  | Массив объектов | Комментарии                                                                           |
| `attachments`                               | Массив объектов | Вложения к сущности                                                                   |
| `keyResultItems`                            | Массив объектов | Ключевые результаты цели                                                              |

---

## Структура `checklistItems`

| Поле                                     | Тип        | Описание                                         |
|------------------------------------------|------------|--------------------------------------------------|
| `text`<span style="color: red;">*</span> | Строка     | Текст чеклиста                                   |
| `checked`                                | Логический | Отметка о выполнении пункта                      |
| `assignee`                               | Строка     | Логин исполнителя пункта чеклиста                |
| `deadline`                               | Строка     | Дедлайн в формате `YYYY-MM-DDThh:mm:ss.sss+hhmm` |

---

## Структура `links`

| Поле                                             | Тип    | Описание                                        |
|--------------------------------------------------|--------|-------------------------------------------------|
| `entity`<span style="color: red;">*</span>       | Строка  | Идентификатор (`id`) связываемой сущности      |
| `relationship`<span style="color: red;">*</span> | Строка | Тип связи: <br>`parent entity` - родительская цель; <br>`child entity` - подцель; <br>`depends on` - текущая цель зависит от связанной; <br>`is dependent by` - текущая цель блокирует связанную; <br>`is supported by` - связь с проектом. |

---

## Структура `comments`

| Поле                                     | Тип           | Описание                        |
|------------------------------------------|---------------|---------------------------------|
| `text`<span style="color: red;">*</span> | Строка        | Текст комментария               |
| `summonees`                              | Массив строк  | Логины призванных пользователей |

---

## Структура `attachments`

| Поле                                         | Тип    | Описание                                                        |
|----------------------------------------------|--------|-----------------------------------------------------------------|
| `filename`<span style="color: red;">*</span> | Строка | Название файла в директории                                     |
| `name`                                       | Строка | Имя файла, с которым он будет храниться на сервере. Если параметр не указан, будет использовано собственное имя файла |

---

## Структура `keyResultItems`

| Поле                                     | Тип        | Описание                               |
|------------------------------------------|------------|----------------------------------------|
| `type`<span style="color: red;">*</span> | Строка     | Тип ключевого результата: <br>`binary` - цель либо достигнута, либо нет; <br>`value` - цель измеряется по значению, например прогресс от 0 до 100 |
| `text`<span style="color: red;">*</span> | Строка     | Текст ключевого результата             |
| `assignee`                               | Строка     | Логин исполнителя ключевого результата |
| `deadline`                               | Объект     | Дедлайн                                |
| `progress`                               | Объект     | Прогресс                               |
| `achieved`                               | Логический | Ключевой результат достигнут           |

---

## Структура `deadline`

| Поле                                             | Тип         | Описание                                                            |
|--------------------------------------------------|-------------|---------------------------------------------------------------------|
| `date`<span style="color: red;">*</span>         | Строка      | Дата дедлайна в формате `YYYY-MM-DDThh:mm:ss.sss+hhmm`              |
| `deadlineType`<span style="color: red;">*</span> | Строка      | Тип дедлайна: <br>`quarter` - квартал, <br>`date` - конкретная дата |
| `isExceeded`                                     | Логический  | Флаг истечения срока                                                |

---

## Структура `progress`

| Поле                                      | Тип                      | Описание           |
|-------------------------------------------|--------------------------|--------------------|
| `start`<span style="color: red;">*</span> | Число с плавающей точкой | Начальный прогресс |
| `end`<span style="color: red;">*</span>   | Число с плавающей точкой | Конечный прогресс  |
| `current`                                 | Число с плавающей точкой | Текущий прогресс   |

---

## Пример `goal.yaml`

```yaml
summary: Increase Sales by 20%
id: goalId
queues: SALES_QUEUE
teamAccess: false
description: |
  Goal: Increase sales by 20% within the quarter.
  Important: Focus on attracting new clients and improving conversion.
markupType: md
author: author_login
lead: sales_lead_login
teamUsers:
  - sales_user1
  - sales_user2
clients:
  - marketing_client
followers:
  - manager_login
start: "2024-01-01T09:00:00.000+0300"
end: "2024-03-31T18:00:00.000+0300"
tags:
  - Q1
  - Sales
entityStatus: according_to_plan
parentEntity: portfolioId
checklistItems:
  - text: Develop a promotion plan
    checked: true
    assignee: sales_user1
    deadline: "2024-01-15T18:00:00.000+0300"
  - text: Conduct 5 promotional campaigns
    checked: false
    assignee: marketing_user1
links:
  - entity: goalId2
    relationship: is supported by
comments:
  - text: "The goal is relevant, need to activate marketing actions"
    summonees:
      - marketing_lead_login
attachments:
  - name: sales_plan.pdf
keyResultItems:
  - type: binary
    text: Launch a new advertising campaign
    assignee: marketing_user1
    deadline:
      date: "2024-02-15T18:00:00.000+0300"
      deadlineType: date
    progress:
      start: 0
      end: 1
      current: 0.5
    achieved: false
  - type: value
    text: Reach a 25% sales growth in March
    assignee: sales_user2
    deadline:
      date: "2024-03-31T18:00:00.000+0300"
      deadlineType: date
    progress:
      start: 55.3
      end: 75.5
      current: 68.9
    achieved: false
```

---

## Особенности работы

  **Вложения**: Все вложения, указанные в данных, должны находиться в директории `attachments` внутри директории цели.
  **Теги**: При импорте в тегах интерфейса Яндекс Трекера будет отображаться тег `imported_<id>`.
