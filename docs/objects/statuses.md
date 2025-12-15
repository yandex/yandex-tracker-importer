# Типы задач

**Файл для импорта:** `statuses.yaml`  
**Расположение:** `./data/statuses.yaml`

> Примечание: Поля, отмеченные звёздочкой (<span style="color: red;">*</span>), являются обязательными.

## Общие сведения

| Поле                                     | Тип    | Описание                        |
|------------------------------------------|--------|---------------------------------|
| `key`<span style="color: red;">*</span>  | Строка | Уникальный ключ статуса         |
| `name`<span style="color: red;">*</span> | Объект | Название статуса на двух языках |
| `type`<span style="color: red;">*</span> | Строка | Тип статуса. Может принимать одно из следующих значений: <br>`new` — Начальный; <br>`inProgress` — В процессе; <br>`paused` — На паузе; <br>`done` — Завершен; <br>`cancelled` — Отменен. |
| `description`                            | Строка | Описание статуса                |

---

## Пример `statuses.yaml`

```yaml
- key: demonstrationToTheCustomer
  name: 
    ru: Демонстрация заказчику
    en: Demonstration to the customer
  type: imProgress
  description: Demonstration to the customer
- key: payment
  name:
    ru: Оплата
    en: Payment
  type: paused
```
