# ID группы "Все сотрудники"
ALL_PARTICIPANTS_GROUP_ID = 1

# Адрес веб-интерфейса Трекера по умолчанию, используется в ссылках на вложения внутри текстов
DEFAULT_TRACKER_UI_URL = "https://tracker.yandex.ru"

# Поля задачи, которые API импорта не принимает: они вычисляются Трекером самостоятельно.
# Если передать их в запрос, задача не будет создана: ответ 400 Incorrect data format.
# remotelinks полем не является вовсе — внешние ссылки живут в отдельном ресурсе задачи.
NON_IMPORTABLE_ISSUE_FIELDS = frozenset({
    "sla",
    "emailFrom",
    "emailTo",
    "emailCc",
    "emailCreatedBy",
    "descriptionHtml",
    "commentWithExternalMessageCount",
    "commentWithoutExternalMessageCount",
    "lastCommentUpdatedAt",
    "previousQueue",
    "previousStatus",
    "previousStatusLastAssignee",
    "parent",
    "epic",
    "remotelinks",
})
