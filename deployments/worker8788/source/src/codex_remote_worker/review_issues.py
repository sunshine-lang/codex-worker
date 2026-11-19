"""Build the downstream v1 review-issues.json from the final project result."""
import re
from collections import Counter

from .contextual_tm import tm_protected_error

FILENAME = 'review-issues.json'
SEMANTIC_CODES = {'revise': 'SEMANTIC_REVISE', 'needs_review': 'SEMANTIC_NEEDS_REVIEW'}
TM_NOTES = {
    'MULTIPLE_TM_100': '第一条 100% TM 优先，保持原样。',
    'TM_SOURCE_DIFFERENCE': '按上游 100% 匹配率复用，不重新计算匹配率。',
}


def group_semantic_issues(issues, allowed):
    """Check the model's data before associating it with the reviewed version."""
    if not isinstance(issues, list) or len(issues) > 8 * len(allowed):
        raise ValueError('Expected at most eight semantic issues per reviewed row.')
    grouped = {}
    seen = set()
    for item in issues:
        if not isinstance(item, dict) or set(item) != {'row_id', 'decision', 'message'}:
            raise ValueError('Each issue requires row_id, decision and message.')
        key, decision, message = item['row_id'], item['decision'], item['message']
        if (not isinstance(key, str) or key not in allowed or not isinstance(decision, str)
                or decision not in SEMANTIC_CODES or not isinstance(message, str)
                or not message.strip() or len(message) > 1000):
            raise ValueError('Issue row, decision or message is invalid.')
        issue = (key, decision, message.strip())
        row_issues = grouped.setdefault(key, [])
        if issue in seen or len(row_issues) == 8:
            raise ValueError('Duplicate issue or too many semantic issues for this row.')
        seen.add(issue)
        row_issues.append({**item, 'message': issue[2]})
    return grouped


def _word_pattern(value):
    before = r'(?<![A-Za-z0-9])' if re.match(r'[A-Za-z0-9]', value) else ''
    after = r'(?![A-Za-z0-9])' if re.search(r'[A-Za-z0-9]$', value) else ''
    return before + re.escape(value) + after


def _missing_terms(record, target):
    seen = set()
    for term in record['tb_list']:
        if term.get('enabled') is False:
            continue
        source = term['source_text'].strip()
        expected = term['target_text'].strip()
        surface = term.get('source_surface')
        surface = surface.strip() if isinstance(surface, str) and surface.strip() else source
        key = (source, surface, expected)
        if key in seen:
            continue
        seen.add(key)
        count = max(1, len(re.findall(_word_pattern(surface), record['source_text'], re.IGNORECASE)))
        options = {word.strip() for word in re.split(r'\s+/\s+', expected) if word.strip()}
        pattern = '|'.join(_word_pattern(word) for word in sorted(options, key=lambda word: (-len(word), word)))
        if len(re.findall(pattern, target)) < count:
            yield f'TERM_MISMATCH: 缺少生效术语或其出现次数不足：{source} → {expected}'


def build_review_report(plan, values, semantic, redact):
    issues, notes = [], []
    plan_notes = {}
    for note in plan['notes']:
        code = note['code']
        if code in TM_NOTES:
            plan_notes.setdefault(note['row_id'], []).append((code, TM_NOTES[code]))
    for index, record in enumerate(plan['records']):
        key = str(index)
        identity = {'row_id': key, 'segment_id': str(record.get('segment_id', index + 1))}
        issue_identity = {**identity, 'row_number': index + 1}
        target = values[key]
        for code, message in plan_notes.get(key, ()):
            notes.append({**identity, 'code': code, 'message': message})
        for message in _missing_terms(record, target):
            if key in plan['locked']:
                notes.append({**identity, 'code': 'TM_100_OVERRIDES_TB', 'message': redact(message)})
            else:
                issues.append({**issue_identity, 'category': 'rule', 'code': 'TERM_MISMATCH',
                               'severity': 'error', 'message': redact(message)})
        protected = tm_protected_error(record, target)
        if protected:
            issues.append({**issue_identity, 'category': 'rule', 'code': 'PROTECTED_CONTENT',
                           'severity': 'warning', 'message': 'PROTECTED_CONTENT: ' + protected + '。'})
        for item in semantic.get(key, ()):
            issues.append({**issue_identity, 'category': 'semantic',
                           'code': SEMANTIC_CODES[item['decision']], 'severity': 'error',
                           'message': redact(item['message'])})
    return {'review_status': 'needs_review' if issues else 'passed', 'issues': issues, 'notes': notes}
