### Импортируемые объекты

- [Очереди](./objects/queues.md)
- [Задачи](./objects/issues.md)
- [Глобальные поля](./objects/global_fields.md)
- [Типы задач](./objects/issue_types.md)
- [Резолюции](./objects/resolutions.md)
- [Статусы](./objects/statuses.md)
- [Категории полей](./objects/categories.md)
- [Проекты](./objects/projects.md)
- [Портфели](./objects/portfolios.md)
- [Цели](./objects/goals.md)

## Пример структуры файлов с данными

```
data
├── goals
│   └── TESTGOAL
│       ├── goal.yaml
│       └── attachments
│           ├── attachment_1.jpg
│           └── attachment_2.jpg
├── projects
│   └── TESTPROJECT
│       ├── project.yaml
│       └── attachments
│           ├── attachment_1.jpg
│           └── attachment_2.jpg
├── portfolios
│   └── TESTPORTFOLIO
│       ├── portfolio.yaml
│       └── attachments
│           ├── attachment_1.jpg
│           └── attachment_2.jpg
├── queues
│   ├── TESTQUEUE
│   │   ├── queue.yaml 
│   │   ├── TESTQUEUE-1
│   │   │   ├── attachments
│   │   │   │   ├── attachment_1.jpg
│   │   │   │   ├── attachment_2.jpg
│   │   │   └── issue.yaml
│   │   └── TESTQUEUE-2
│   │       ├── attachments
│   │       │   └── attachment_3.xls
│   │       └── issue.yaml
│   └──  NEWTESTQUEUE
│        └── queue.yaml
├── global_fields.yaml
├── issue_types.yaml
├── resolutions.yaml
├── statuses.yaml
└── categories.yaml
```

# Конфигурационный файл

**Конфигурация хранится в файле:** `./config.yaml`

Доступные параметры:

| Ключ                 | Значение                                                    |
|----------------------|-------------------------------------------------------------|
| `dest_tracker_api`   | Адрес API Яндекс Трекера (`https://api.tracker.yandex.net`) |
| `dest_tracker_token` | Токен для доступа к Яндекс Трекеру                          |
| `dest_org_id`        | ID организации для организаций из 360                       |
| `dest_cloud_org_id`  | ID организации для организаций из Яндекс.Облака             |

**Примечание:** только один из ID организации должен быть установлен. Остальные параметры обязательны.

**Пример файла `config.yaml`:**

```yaml
dest_tracker_api: https://api.tracker.yandex.net
dest_tracker_token: y0__xD**
dest_org_id: "12345***"
```

# Сопоставление

Используется для сопоставления объектов из исходной системы с объектами целевой системы.

**Файлы с сопоставлениями:**
- `queues.cfg` — для сопоставления ключей очередей.
- `users.cfg` — для сопоставления логинов пользователей.
- `statuses.cfg` — для сопоставления ключей статусов.
- `resolutions.cfg` — для сопоставления ключей резолюций.
- `issue_types.cfg` — для сопоставления ключей типов задач.
- `priorities.cfg` — для сопоставления ключей приоритетов.
- `fields.cfg` — для сопоставления ключей полей.

**Формат файлов для сопоставления:**
Каждая строка содержит сопоставление в формате:
```
<исходный_ключ>:<целевой_ключ>
```

Пример для `queues.cfg`:
```
COMM:COMMERCE
MARKET:MARKETING
```

**Пример структуры:**

```
mapping
├── queues.cfg
├── users.cfg
├── statuses.cfg
├── resolutions.cfg
├── issue_types.cfg
├── priorities.cfg
└── fields.cfg
```

Если для ключа нет сопоставления, то он используется в исходном виде.

### **Разбиение импорта на этапы**  

Если вам нужно разделить импорт данных на несколько этапов, при каждом новом импорте добавляйте файлы с данными, которые требуется доимпортировать.

**Важно:**  
- Не удаляйте уже загруженные файлы — они могут содержать информацию, необходимую для связей между объектами из предыдущих итераций импорта.  

#### **Какие объекты можно импортировать поэтапно:**  
- Проекты
- Портфели
- Цели
- Очереди
- Компоненты
- Версии
- Локальные поля
- Триггеры
- Макросы
- Задачи

#### **Какие объекты нельзя импортировать поэтапно:**  
- Вложения
- Комментарии
- Чеклисты
- Ворклоги
