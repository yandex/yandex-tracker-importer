# Резолюции

**Файл для импорта:** `resolutions.yaml`  
**Расположение:** `./data/resolutions.yaml`

> Примечание: Поля, отмеченные звёздочкой (<span style="color: red;">*</span>), являются обязательными.

## Общие сведения

| Поле                                     | Тип    | Описание                          |
|------------------------------------------|--------|-----------------------------------|
| `key`<span style="color: red;">*</span>  | Строка | Уникальный ключ резолюции.        |
| `name`<span style="color: red;">*</span> | Объект | Название резолюции на двух языках |
| `description`                            | Строка | Описание резолюции.               |

---

## Пример `resolutions.yaml`

```yaml
- key: notProfitable
  name:
    ru: Не выгодно
    en: Not profitable
  description: Not profitable task
- key: notEnoughBudget
  name: 
    ru: Недостаточно бюджета
    en: Not enough budget
```
