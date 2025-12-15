# Глобальные поля

**Файл для импорта:** `global_feilds.yaml`  
**Расположение:** `./data/global_fields.yaml`

> Примечание: Поля, отмеченные звёздочкой (<span style="color: red;">*</span>), являются обязательными.

## Общие сведения

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

## Пример `global_fields.yaml`

```yaml
- category: Системные
  description: Trusted persons
  key: trustedPersons
  name:
    en: Users
    ru: Пользователи
  schema:
    type: array
    items: user
  optionsProvider:
    type: FixedUserListOptionsProvider
- category: Учет Времени
  description: Project completion date
  key: completionDate
  name:
    en: Completion project
    ru: Завершение проекта
  schema:
    type: date
- category: Офисы
  key: locationOffices
  name:
    en: Location office
    ru: Расположение офиса
  schema:
    type: array
    items: string
  optionsProvider:
    type: FixedListOptionsProvider
    values:
    - office_1
    - office_2
```

---
