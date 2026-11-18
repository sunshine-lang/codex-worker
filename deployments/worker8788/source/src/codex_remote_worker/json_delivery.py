"""JSON task input and final-message delivery; no spreadsheet conversion."""
import json
import math
import re

from .common import JobError
from .contextual_tm import CONTEXTUAL_TM, contextual_candidates, resolve_tm_choice
from .tm_policy import PROJECT_CONTRACT, select_tm100

CONTRACT = 'siemens-json/1'
JSON_CONTRACTS = (CONTRACT, PROJECT_CONTRACT)


def load_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('nonfinite number')
        return value

    def constant(text):
        raise ValueError('nonstandard JSON constant')

    value = json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=constant)
    # Reject escaped lone surrogates before a normalized UTF-8 artifact is written.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            item.encode('utf-8', errors='strict')
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


def text(value):
    return isinstance(value, str) and bool(value.strip())


def read_json_source(path):
    try:
        packet = load_json(path.read_text(encoding='utf-8-sig'))
        if not isinstance(packet, dict):
            raise ValueError('invalid packet')
        project = packet.get('contract') == PROJECT_CONTRACT
        keys = {'contract', 'phase', 'rows', 'project_name', 'request_id', 'tm_reuse'} if project else {'contract', 'phase', 'rows'}
        contextual = project and packet.get('tm_policy') == CONTEXTUAL_TM and packet.get('phase') == 'translate'
        if contextual:
            keys.add('tm_policy')
        if (set(packet) != keys or packet['contract'] not in JSON_CONTRACTS
                or packet['phase'] not in {'translate', 'review'} or not isinstance(packet['rows'], list)):
            raise ValueError('invalid packet')
        reused = packet.get('tm_reuse', [])
        if not isinstance(reused, list) or not 1 <= len(packet['rows']) + len(reused) <= 10000:
            raise ValueError('invalid rows')
        if project and (not text(packet['project_name']) or len(packet['project_name']) > 200
                        or not text(packet['request_id']) or len(packet['request_id']) > 128):
            raise ValueError('invalid project metadata')
        ids = []
        for row in packet['rows'] + reused:
            if not isinstance(row, dict) or not text(row.get('row_id')) or not isinstance(row.get('record'), dict):
                raise ValueError('invalid row')
            if project and not re.fullmatch(r'0|[1-9][0-9]*', row['row_id']):
                raise ValueError('invalid global row ID')
            ids.append(row['row_id'])
            record = row['record']
            if (record.get('source_language') != 'en' or record.get('target_language') != 'zh-CN'
                    or not text(record.get('source_text'))):
                raise ValueError('invalid source')
            for key in ('tb_list', 'tm_list'):
                if not isinstance(record.get(key), list) or any(
                    not isinstance(item, dict) or not text(item.get('source_text')) or not text(item.get('target_text'))
                    for item in record[key]
                ):
                    raise ValueError('invalid references')
            if packet['phase'] == 'review' and not text(record.get('target_text')):
                raise ValueError('missing candidate')
        if project:
            if contextual and reused:
                raise ValueError('contextual work packets contain only Agent rows')
            for row in packet['rows']:
                if contextual:
                    if contextual_candidates(row['record']) != row['record']['tm_list']:
                        raise ValueError('invalid contextual reference identities or ordering')
                elif select_tm100(row['record']) is not None:
                    raise ValueError('100% TM must not enter model rows')
            for row in reused:
                selected = select_tm100(row['record'])
                selection = row.get('selection')
                # bool compares equal to 0/1 in Python; reject forged typed selections.
                if (not isinstance(selection, dict) or type(selection.get('tm_index')) is not int
                        or type(selection.get('match_rate')) not in (int, float)
                        or type(selection.get('multiple_targets')) is not bool
                        or type(selection.get('source_exact')) is not bool
                        or selected is None or selection != selected
                        or row['record'].get('target_text') != selected['target_text']):
                    raise ValueError('invalid locked TM selection')
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate row IDs')
        return packet
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise JobError('INVALID_JSON_SOURCE', '输入必须是有效的 siemens-json/1 或 siemens-json/2 翻译或审校任务。') from exc


def check_json_result(packet, result):
    try:
        if not isinstance(result, dict) or set(result) != {'rows'} or not isinstance(result['rows'], list):
            raise ValueError('expected rows')
        rows = result['rows']
        expected = {row['row_id']: row for row in packet['rows']}
        if (len(rows) != len(expected) or any(not isinstance(row, dict) or not text(row.get('row_id')) for row in rows)
                or {row['row_id'] for row in rows} != set(expected)):
            raise ValueError('missing or duplicate rows')
        issues = []
        for row in rows:
            if packet['phase'] == 'translate':
                keys = {'row_id', 'target_text', 'term_decisions'}
                if packet.get('tm_policy') == CONTEXTUAL_TM:
                    keys.add('tm_decision')
                    resolve_tm_choice(expected[row['row_id']]['record'], row['target_text'], row.get('tm_decision'))
                if set(row) != keys or not text(row['target_text']):
                    raise ValueError('invalid translation')
                terms = row['term_decisions']
                if not isinstance(terms, list) or any(
                    not isinstance(term, dict) or set(term) != {'source_text', 'target_text'}
                    or not all(text(value) for value in term.values()) for term in terms
                ):
                    raise ValueError('invalid terms')
            else:
                if (set(row) != {'row_id', 'decision', 'issues', 'coverage', 'new_senses'}
                        or row['decision'] not in {'pass', 'revise', 'needs_review'}
                        or not isinstance(row['issues'], list) or not all(text(issue) for issue in row['issues'])
                        or not isinstance(row['coverage'], list) or not isinstance(row['new_senses'], list)):
                    raise ValueError('invalid review')
                if (row['decision'] == 'pass') != (not row['issues']):
                    raise ValueError('inconsistent decision')
                for item in row['coverage']:
                    if (not isinstance(item, dict) or set(item) != {'id', 'evidence'}
                            or type(item['id']) is not int or not text(item['evidence'])):
                        raise ValueError('invalid coverage')
                for item in row['new_senses']:
                    if (not isinstance(item, dict) or set(item) != {'source_text', 'target_text', 'reason'}
                            or not all(text(value) for value in item.values())):
                        raise ValueError('invalid sense')
                issues.extend({'id': row['row_id'], 'rule': 'independent_review', 'severity': 'error', 'message': issue}
                              for issue in row['issues'])
        return {'rows': len(rows), 'errors': len(issues), 'warnings': 0, 'issues': issues,
                'language_quality': 'awaiting_host_validation'}
    except (ValueError, TypeError, KeyError) as exc:
        raise JobError('INVALID_JSON_RESULT', '最终回复必须是与输入行号对应的纯 JSON 译文或独立审校结论。') from exc
