# Типы задач

**Файл для импорта:** `issue_types.yaml`  
**Расположение:** `./data/issue_types.yaml`

> Примечание: Поля, отмеченные звёздочкой (<span style="color: red;">*</span>), являются обязательными.

## Общие сведения

| Поле                                     | Тип    | Описание                            |
|------------------------------------------|--------|-------------------------------------|
| `key`<span style="color: red;">*</span>  | Строка | Уникальный ключ типа задачи         |
| `name`<span style="color: red;">*</span> | Объект | Название типа задачи на двух языках |
| `description`                            | Строка | Описание типа задачи                |

---

## Пример `issue_types.yaml`

```yaml
- name: 
    en: Study
    ru: Исследование
  key: study
  description: Search for patterns
- name:
    ru: Командировка
    en: Business trip
  key: businessTrip
```
