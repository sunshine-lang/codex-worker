"""Contextual TM reference identities, routing and binding of final decisions."""
from collections import Counter
import hashlib
import json
import math
import re
import unicodedata


CONTEXTUAL_TM = 'contextual-v2'
TM_ID_FIELDS = ('id', 'tm_id', 'tm_target_id', 'resource_id', 'source_file', 'source_text',
                'target_text', 'source_surface', 'context', 'source_language', 'target_language', 'match_rate')
UNIT_PATTERN = r'(\d+(?:\.\d+)?)\s*(kV|mV|V|mA|kA|A|MHz|kHz|Hz|kW|mW|W|MPa|kPa|Pa|ms|mm|cm|kg)(?![A-Za-z])'


def tm_candidate_id(ref):
    identity = {k: ref[k] for k in TM_ID_FIELDS if ref.get(k) is not None}
    if isinstance(identity.get('match_rate'), float) and identity['match_rate'].is_integer():
        identity['match_rate'] = int(identity['match_rate'])
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return 'tm_' + hashlib.sha256(raw.encode()).hexdigest()[:24]


def contextual_candidates(record):
    candidates = {}
    for ref in record['tm_list']:
        if ref.get('enabled') is False:
            continue
        rate = ref.get('match_rate')
        if rate is not None and (type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 100):
            raise ValueError('INVALID_TM_RATE: TM 匹配率必须在 0～100 之间。')
        key = tm_candidate_id(ref)
        previous = candidates.get(key)
        if previous and any(previous.get(k) != ref.get(k) for k in TM_ID_FIELDS):
            raise ValueError('TM_ID_CONFLICT: 不同候选产生了相同的编号。')
        candidates[key] = {**ref, 'candidate_id': key}
    return sorted(candidates.values(), key=lambda r: (-(r['match_rate'] if r.get('match_rate') is not None else -1),
                                                       r['candidate_id']))


def same_reference_languages(record, ref):
    # The existing en→zh contract treats zh and zh-CN as the same target.
    def normalized(value):
        return 'zh-cn' if str(value).lower() in ('zh', 'zh-cn') else str(value).lower()
    return all(not ref.get(k) or normalized(ref[k]) == normalized(record[k])
               for k in ('source_language', 'target_language'))


def tm_terms_present(record, target):
    for term in record['tb_list']:
        if term.get('enabled') is False:
            continue
        alternatives = re.split(r'\s+/\s+', term['target_text'])
        def present(word):
            left = r'(?<![A-Za-z0-9])' if re.match(r'[A-Za-z0-9]', word) else ''
            right = r'(?![A-Za-z0-9])' if re.search(r'[A-Za-z0-9]$', word) else ''
            return bool(re.search(left + re.escape(word) + right, target))
        if not any(present(word.strip()) for word in alternatives if word.strip()):
            return False
    return True


def tm_protected_error(record, target):
    source = record['source_text']
    pattern = r'\$?\{\{?[^{}]*\}\}?|%(?:\([^)]+\)|\d+\$)?[-+#0 ]*(?:\d+)?(?:\.\d+)?[sdifouxXeEgGc]|<[^<>\n]+>'
    tags = re.findall(pattern, source)
    other_tags = re.findall(pattern, target)
    if Counter(tags) != Counter(other_tags):
        return '占位符或标签发生变化'
    # A matching multiset also has to preserve the nesting of paired markup.
    def nesting(tokens):
        stack, paths = [], []
        for token in tokens:
            tag = re.fullmatch(r'<(/?)([\w:-]+)(?:\s[^<>]*)?/?>', token)
            if not tag or token.endswith('/>'):
                continue
            closing, name = tag.groups()
            if closing:
                if not stack or stack.pop() != name:
                    return None
            else:
                paths.append((*stack, name))
                stack.append(name)
        return None if stack else Counter(paths)
    source_nesting = nesting(tags)
    if source_nesting is not None and nesting(other_tags) != source_nesting:
        return '标签嵌套发生变化'
    def numbers(value):
        value = unicodedata.normalize('NFKC', re.sub(pattern, ' ', value)).replace('−', '-')
        found = re.findall(r'[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)*', value)
        return Counter(n.replace(',', '') for n in found)
    before, after = numbers(source), numbers(target)
    if before - after or set(after) - set(before):
        return '技术数值发生变化'
    src_units, dst_units = set(re.findall(UNIT_PATTERN, source)), set(re.findall(UNIT_PATTERN, target))
    if {n for n, _ in src_units - dst_units} & {n for n, _ in dst_units - src_units}:
        return '相同数值的技术单位发生变化'
    resources = r'https?://[^\s<>"，。]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:[A-Za-z]:\\|/)(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+'
    if Counter(s.rstrip('.,;:') for s in re.findall(resources, source)) != Counter(s.rstrip('.,;:') for s in re.findall(resources, target)):
        return '路径、URL 或邮箱发生变化'
    symbol = r'(?<![a-zA-Z0-9_])[A-Z]{2,}(?![a-zA-Z0-9_])'
    src, dst = set(re.findall(symbol, source)), set(re.findall(symbol, target))
    allowed = {v for term in record['tb_list'] if term.get('enabled') is not False
               for v in re.findall(symbol, term['target_text'])}
    if src - dst and dst - src - allowed:
        return '技术缩写被替换为当前原文和术语未指定的另一缩写'
    return None


def contextual_auto(record):
    hits = [r for r in contextual_candidates(record) if r.get('match_rate') == 100]
    context = record.get('context')
    if (not isinstance(context, str) or context.strip().casefold() in
            ('', '-', 'n/a', 'none', 'null', 'unknown', 'ui', 'button', '按钮', '未知', '无', '无上下文') or
            len({r['target_text'] for r in hits}) != 1):
        return None
    for ref in hits:
        target = ref['target_text']
        if (ref['source_text'] != record['source_text'] or ref.get('context') != context
                or not same_reference_languages(record, ref) or tm_protected_error(record, target)):
            continue
        if str(record.get('target_language', '')).lower().startswith('zh') and re.search(r'[a-zA-Z]', target) and not re.search(r'[\u3400-\u9fff]', target):
            # A product name may legitimately stay in Latin script; let the Agent verify its role.
            continue
        if Counter(re.findall(UNIT_PATTERN, record['source_text'])) - Counter(re.findall(UNIT_PATTERN, target)):
            continue
        if not tm_terms_present(record, target):
            continue
        # Treat unmatched product identifiers and acronyms as a reason to consult the Agent.
        code = r'(?<![A-Za-z0-9_])(?:[A-Z]{2,}|[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+|(?=[A-Za-z0-9.-]*[A-Za-z])(?=[A-Za-z0-9.-]*\d)[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)(?![A-Za-z0-9_])'
        if Counter(re.findall(code, record['source_text'])) != Counter(re.findall(code, target)):
            continue
        return ref
    return None


def resolve_tm_choice(record, target, decision, *, fill_reuse=False):
    if (not isinstance(decision, dict) or set(decision) != {'action', 'candidate_id'}
            or decision['action'] not in ('reuse', 'adapt', 'retranslate')
            or not isinstance(decision['candidate_id'], str) or not isinstance(target, str)):
        raise ValueError('TM_DECISION_INVALID: 需要 action 和 candidate_id。')
    action, key = decision['action'], decision['candidate_id']
    hits = [r for r in record['tm_list'] if r.get('enabled') is not False and r.get('match_rate') == 100]
    if action == 'retranslate':
        if key:
            raise ValueError('TM_DECISION_INVALID: 重新翻译的 candidate_id 应为空。')
    else:
        ref = next((r for r in hits if tm_candidate_id(r) == key), None)
        if ref is None or not same_reference_languages(record, ref):
            raise ValueError('TM_DECISION_INVALID: 候选编号或语言对不属于本句的生效 100% TM。')
        if action == 'reuse':
            if fill_reuse and target == '':
                target = ref['target_text']
            if target != ref['target_text']:
                raise ValueError('TM_REUSE_CHANGED: 原样复用必须等于被选候选的译文。')
            if not tm_terms_present(record, target):
                raise ValueError('TM_TERM_CONFLICT: 被选候选缺少本句的生效术语，请调整译文或重新翻译。')
        elif target == ref['target_text']:
            raise ValueError('TM_DECISION_INVALID: 未改动被选候选时应使用 reuse。')
    if not target.strip():
        raise ValueError('EMPTY_TRANSLATION: 缺少完整译文。')
    problem = tm_protected_error(record, target) if hits else None
    if problem:
        raise ValueError('PROTECTED_CONTENT: ' + problem)
    return target


def tm_receipt(record, actor, action, candidate_id):
    refs = [r for r in record['tm_list'] if r.get('enabled') is not False]
    index = next((i for i, r in enumerate(refs) if tm_candidate_id(r) == candidate_id), None) if candidate_id else None
    if candidate_id and index is None:
        raise ValueError('TM_DECISION_INVALID: 被选候选不属于原始输入。')
    return {'segment_id': record.get('segment_id'), 'actor': actor, 'action': action,
            'candidate_id': candidate_id, 'enabled_tm_index': index, 'upstream_id': refs[index].get('id') if index is not None else None}


def verify_tm_report(records, targets, decisions, direct):
    direct = set(direct)
    expected = {str(i) for i, row in enumerate(records)
                if any(r.get('enabled') is not False and r.get('match_rate') == 100 for r in row['tm_list'])}
    if not isinstance(decisions, dict) or set(decisions) != expected or not direct <= expected:
        raise ValueError('TM_DECISION_INVALID: 决策清单未覆盖正确的句段。')
    counts = {'direct_reused': 0, 'agent_reused': 0, 'agent_adapted': 0, 'agent_retranslated': 0}
    for key, item in decisions.items():
        i = int(key)
        if (not isinstance(item, dict) or type(item.get('enabled_tm_index')) is bool
                or item != tm_receipt(records[i], 'rule' if key in direct else 'agent',
                                      item.get('action'), item.get('candidate_id'))):
            raise ValueError('TM_DECISION_INVALID: 句段或决策来源不匹配。')
        choice = {'action': item['action'], 'candidate_id': item['candidate_id']}
        resolve_tm_choice(records[i], targets[i], choice)
        if item['actor'] == 'rule':
            ref = contextual_auto(records[i])
            if ref is None or choice != {'action': 'reuse', 'candidate_id': ref['candidate_id']}:
                raise ValueError('TM_DECISION_INVALID: 直接复用缺乏匹配的证据。')
            counts['direct_reused'] += 1
        else:
            counts[{'reuse': 'agent_reused', 'adapt': 'agent_adapted', 'retranslate': 'agent_retranslated'}[choice['action']]] += 1
    return counts
