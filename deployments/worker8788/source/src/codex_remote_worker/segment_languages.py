"""Language selection for orders that declare the source and target on each segment."""
from .common import JobError


PER_SEGMENT = 'per-segment'
LANGUAGE_INSTRUCTIONS = (
    '每个句段按其 source_language 和 target_language 翻译。Read each source_text in its own '
    'source_language and write target_text in its target_language, including any requested script or region. '
    'A project can contain several different directions. Honor the pair on each row throughout translation, '
    'revisions and self-check. Keep its language values and segment identity. Use that row\'s TB/TM and the '
    'uploaded prompt and guides for applicable terminology and style. If a document prescribes a different '
    'translation direction, the row fields take precedence. Compare repeated translations within the same pair.'
)


def require_segment_languages(record, position):
    missing = [key for key in ('source_language', 'target_language')
               if not isinstance(record.get(key), str) or not record[key].strip()]
    if missing:
        identifier = record.get('segment_id')
        location = f'segment_id={identifier}' if type(identifier) in (str, int) and str(identifier).strip() else f'第 {position} 条'
        raise JobError('INVALID_LANGUAGE', f'{location} 的 {", ".join(missing)} 必须是非空字符串。')
