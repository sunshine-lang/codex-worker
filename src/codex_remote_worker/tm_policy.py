"""Shared deterministic TM selection. The upstream percentage is authoritative."""
import math

PROJECT_CONTRACT = 'siemens-json/2'


def select_tm100(record):
    candidates=[]
    for index, item in enumerate(record.get('tm_list', [])):
        rate=item.get('match_rate')
        if rate is not None and (type(rate) not in (int,float) or not 0 <= rate <= 100 or not math.isfinite(rate)):
            raise ValueError('INVALID_TM_RATE: match_rate 必须为 0～100 数值或 null。')
        if rate == 100:
            if not isinstance(item.get('target_text'),str) or not item['target_text'].strip():
                raise ValueError('INVALID_TM: 100% TM 的译文不能为空。')
            candidates.append((index,item))
    if not candidates:return None
    index,item=candidates[0]
    return {'tm_index':index,'match_rate':100,'target_text':item['target_text'],
            'multiple_targets':len({entry['target_text'] for _,entry in candidates})>1,
            'source_exact':item.get('source_text')==record.get('source_text')}
