def translit(text: str) -> str:
    """
    Faster transliteration of Russian text to Latin characters

    Args:
        text (str): Russian text to transliterate

    Returns:
        str: Transliterated text in Latin characters
    """

    # Translation tables for single-character replacements
    single_chars = {
        ord('а'): 'a',
        ord('б'): 'b',
        ord('в'): 'v',
        ord('г'): 'g',
        ord('д'): 'd',
        ord('е'): 'e',
        ord('з'): 'z',
        ord('и'): 'i',
        ord('й'): 'y',
        ord('к'): 'k',
        ord('л'): 'l',
        ord('м'): 'm',
        ord('н'): 'n',
        ord('о'): 'o',
        ord('п'): 'p',
        ord('р'): 'r',
        ord('с'): 's',
        ord('т'): 't',
        ord('у'): 'u',
        ord('ф'): 'f',
        ord('ы'): 'y',
        ord('э'): 'e',
        ord('А'): 'A',
        ord('Б'): 'B',
        ord('В'): 'V',
        ord('Г'): 'G',
        ord('Д'): 'D',
        ord('Е'): 'E',
        ord('З'): 'Z',
        ord('И'): 'I',
        ord('Й'): 'Y',
        ord('К'): 'K',
        ord('Л'): 'L',
        ord('М'): 'M',
        ord('Н'): 'N',
        ord('О'): 'O',
        ord('П'): 'P',
        ord('Р'): 'R',
        ord('С'): 'S',
        ord('Т'): 'T',
        ord('У'): 'U',
        ord('Ф'): 'F',
        ord('Ы'): 'Y',
        ord('Э'): 'E',
        # Remove hard and soft signs
        ord('ъ'): '',
        ord('ь'): '',
        ord('Ъ'): '',
        ord('Ь'): '',
    }

    # Multi-character replacements need to be handled separately
    multi_chars = {
        'ё': 'yo',
        'ж': 'zh',
        'х': 'kh',
        'ц': 'ts',
        'ч': 'ch',
        'ш': 'sh',
        'щ': 'shch',
        'ю': 'yu',
        'я': 'ya',
        'Ё': 'Yo',
        'Ж': 'Zh',
        'Х': 'Kh',
        'Ц': 'Ts',
        'Ч': 'Ch',
        'Ш': 'Sh',
        'Щ': 'Shch',
        'Ю': 'Yu',
        'Я': 'Ya',
    }

    # First pass: apply single-character translations
    result = text.translate(single_chars)

    # Second pass: replace multi-character combinations
    for cyrillic, latin in multi_chars.items():
        result = result.replace(cyrillic, latin)

    return result
