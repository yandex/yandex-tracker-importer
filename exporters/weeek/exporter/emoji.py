import re

# WEEEK emoji of its own set: id -> the closest Unicode emoji, or `:name:` for an emoji without one.
# The `alt` of these emoji in a text is a placeholder character.
WEEEK_EMOJI = {
    "9b0529ec-e938-4dfe-918a-8b11ae3d520f": ":w1:",
    "9b0529ef-5a18-4e8c-b46a-449f9c44b714": ":w2:",
    "9b052a1b-4c03-4209-895d-b64b2671b3dd": ":w3:",
    "9b052b7b-9c11-4458-88d9-52ce2b985c39": ":w4:",
    "9b05296f-c2f6-40ba-bb40-59806178ccbc": "😊",
    "9b052951-251f-4b7a-9910-5f51bb3192f1": "😃",
    "9b05293f-bae6-4b9d-9b95-f8efee5229cd": "😆",
    "9b052921-0399-487f-a2fb-2613df5bd469": "😍",
    "9b052906-3225-4839-b87d-0d125b133b60": "😎",
    "9b0528f0-d4d5-4f49-995a-4951db171bd1": "😬",
    "9b0528d4-25bb-43f0-8ca7-a5f1d1c2a0f1": "😳",
    "9b052871-cdc6-4412-ad1e-dcdc200f9480": "😢",
    "9b052824-ffbd-49b1-8f6c-ebf9dca205cc": "😒",
    "9b0527f9-3b9d-4b98-8c1b-afa26ded4d81": "😡",
    "9b0527dd-25ab-4aaf-abf2-7c0dd883d58f": "😵",
    "9b0527ba-da71-45a8-926e-3cd32c7079a6": "🤢",
    "9b05278f-3cb0-4189-98f5-47e2527fd918": "🥳",
    "9b052775-5b77-4ae4-a139-1f9134b45116": "🤡",
    "9b05275b-0710-4500-984a-84da2ef91b51": "👀",
    "9b05273b-0b62-47d9-8075-859850972e1a": "👨‍💻",
    "9b052723-6946-47e7-9dd6-9b38ec20e857": "👩‍💻",
    "9b0526fa-7f56-4211-b650-554ad42a264e": "👍",
    "9b0526d7-8bf0-4a27-b69a-6e975379c8e8": "👎",
    "9b0526ab-63b1-46a9-8e66-97994992bf47": "👻",
    "9b052674-e12d-4f8e-97e5-7f37752e8649": "🔥",
    "9b052651-8f43-4b58-9b51-f73cc2dfee02": "🚀",
    "9b052638-c7b5-4c25-94ac-4ae66e88f7c5": "📅",
    "9b052601-fa61-4f3e-af04-bb6b97dc2a9a": "🙀",
    "9b0525e2-1af8-4dbf-9454-cd0be826dfad": "🐈",
    "9b0525c3-56b7-46ae-ad9e-b23fa81350e5": "🐦",
    "9b05259c-6802-4ac6-be8d-6d083f8000ab": "🐸",
    "9b052533-d1a7-4c8b-84df-2a591a9956c7": "🦖",
    "9b052507-c318-4ce5-94ba-6dc8fe17b0a8": "🌼",
    "9b0524e5-ac8a-4f6a-93ca-ad60a5494316": "🎄",
    "9b0524c2-30e1-4538-8823-fe236fa346e5": "⚡",
    "9b052499-bb42-47f4-8cc8-f53a8e4a1b86": "🌙",
    "9b052473-ab3d-4b69-8a6e-1bff9a71c86b": "✨",
    "9b05244c-2688-483f-b7c4-1829e4ce4868": "⭐",
    "9b052414-dc10-4904-870c-4c11696dcf8c": "🗿",
    "9b0523fd-46fa-4e68-8652-b7f87e2674dc": "💸",
    "9b0523de-06ff-4d85-abe0-5eb8ab917f5a": "🪙",
    "9b0523c4-a430-4103-b12e-12d92d016420": "🏆",
    "9b05239b-f588-46b7-8ab7-7e54c2a0bc10": "☕",
    "9b05233e-a404-43e3-94f3-8d070f7e6f85": "🎁",
    "9b0522b3-97d4-4ab9-bbe0-d5a9eb8b11de": "🎂",
    "9b05227e-3ae5-49d5-9f46-1ffb665a830d": "🔍",
    "9b052260-78e5-48a5-a12e-e409548fe7af": ":crop:",
    "9b05224a-5b17-4940-954d-658ff021bae7": ":f:",
    "9b05222d-e4a5-4fa1-a076-96739e12c890": ":cursor:",
    "9b052200-672a-4284-b7f0-f6f1c4ef160e": ":sobaka:",
    "9b0521a7-577e-47b3-ae3d-c5c5fd638787": "✅",
    "9b052186-5424-410a-800e-c37742ac02cc": "💬",
    "9b052118-c33b-4183-ad1c-93e8871bf7bc": "✉️",
    "9b0520f4-2fd7-44ec-8b2a-7717b0a60987": "📩",
    "9b0520d3-712d-4617-9fde-1ee248367145": "🔒",
    "9b052085-e7d5-4924-b89e-648a73405360": "📈",
    "9b052065-c341-4b44-83be-a2d530fb1f36": "📉",
    "9b052039-7722-4422-b44b-00ba4cde285d": "🔊",
    "9b052003-56cf-4934-8c9e-1d34789e6dfe": "🔇",
    "9b051f79-e951-449e-80f1-7c49f73ccbc6": "❤️‍🔥",
    "9b051f5f-6f04-40a3-8af4-394cddddf67c": "💙",
    "9b051f32-df75-437b-b2f6-0b1a8998b34b": "🖤",
    "9b051f09-39a8-4e97-8f78-36a46cd0c463": "📌",
    "9b051e93-4167-4daa-b2ce-74cac849ae02": "➕",
    "9b051e6f-f8ee-4182-aff1-3b6df92c86ee": "❌",
    "9b051e49-7dfb-41de-9c53-686ab6edc66c": "❗",
    "9b051e16-383b-45a8-99c4-a8fee96d9df7": "🔼",
    "9b051d86-40cd-4f1e-9b12-1008ed452236": "🔻",
    "9b051d3e-abbb-4117-abaa-fcaeae7d5a93": "🟢",
    "9b051d1a-b6e0-4875-ba83-71e2758c9e88": "🔵",
    "9b051908-445b-4c05-90c6-8db4273f4470": "🔴",
}

EMOJI_URL_PREFIX = "weeek://emoji/"

_MARKDOWN_EMOJI = re.compile(r"!\[([^\]]*)\]\(weeek://emoji/([0-9A-Fa-f-]+)\)")


def emoji_text(emoji_id: str, alt: str) -> str:
    """An emoji of the WEEEK set by its id; any other emoji is its `alt`."""
    return WEEEK_EMOJI.get(emoji_id.lower(), alt)


def replace_markdown_emoji(text: str) -> str:
    """``![alt](weeek://emoji/<id>)`` -> the emoji text."""
    return _MARKDOWN_EMOJI.sub(lambda match: emoji_text(match.group(2), match.group(1)), text)
