"""Autonomous whole-project work in one conversation, verified against saved state."""
from __future__ import annotations

import json

from .common import JobError, write_json


class ProjectController:
    version = 'ledger/2'
    max_stalled_turns = 3

    def __init__(self, session):
        self.session = session
        self.history = []
        self.stalled = 0
        self.before = self.measure()
        # A safety ceiling for premature endings, not a per-turn work quota.
        self.max_turns = max(16, len(session.records)*2 + 16)

    def measure(self):
        s = self.session
        return {'phase':s.phase, 'draft':len(s.values),
                'reviewed':s.progress()['reviewed_segments'], 'checks_read':s.checks_read,
                'checks_pages':len(s.checks_seen)}

    def remaining(self):
        s = self.session
        if s.phase == 'translating':
            return [k for k in s.pending if k not in s.values]
        return [str(i) for i in range(len(s.records))
                if s.version(str(i)) is None or s.approved.get(str(i)) != s.version(str(i))]

    def request(self):
        s = self.session
        remaining = self.remaining()
        self.before = self.measure()
        instruction = (
            'Autonomously complete ALL remaining translations AND final self-review in this project in this turn. '
            'Choose how many rows to read and save per tool call; keep making tool calls until the '
            'entire draft is saved. All original source and TB/TM are available through project_read. '
            'You choose the scope: count=0 reads all remaining entries; char_limit=0 returns the full selected '
            'content. Positive count/char_limit values request your chosen range/page size. If you choose pages, follow next_read until '
            'each page is complete. Use the whole project context, choose your translation order, and revise '
            'earlier translations when needed. Do not re-save unchanged rows. Do not end this turn '
            'merely because a tool read is finished. After the entire draft is '
            'saved, project_write opens the reviewing phase. Continue immediately in THIS turn: read all current '
            'source/translation entries, correct errors, acknowledge current versions with project_review, and '
            'read all checks pages. Only end when the entire project is translated and self-reviewed.'
            if s.phase == 'translating' else
            'Autonomously self-review ALL CURRENT source/translation entries in the project against the original prompt, '
            'both guides, meaning, omissions, TB/TM, quantities, codes and placeholders. Correct errors '
            'with project_write, re-read changed versions and acknowledge with project_review. '
            'Do not approve unread versions. Choose whether to read all entries at once or selected ranges '
            'using project_read, and continue within this turn until the entire project is reviewed. '
            'Read ALL project_read checks pages using next_read; address issues '
            'and re-read/review any changed rows. Different contexts can justify different translations. '
            'End this turn only after all current versions and all checks are covered.')
        return json.dumps({'host_controller':self.version, 'project':s.overview(),
            'phase':s.phase, 'work_scope':'whole_remaining_project', 'remaining_rows':len(remaining),
            'materials':{'source':'Complete original entries, context, TB/TM and current translations via project_read source',
                'guides':'Complete original prompt and guides via project_read guides',
                'read_scope':'Model-selected; count=0 means all remaining entries; char_limit=0 means no pagination'},
            'instruction':instruction,
            'continuation':'Continue this project in the current conversation. Saved rows and notes remain available. '
                'Context compaction or finishing one model turn does not finish the project. '
                'If a turn ends early, the host will continue the remaining work in this same conversation. '
                'Only saved translations, current-version review and complete checks establish completion.'}, ensure_ascii=False)

    def completed(self, payload):
        """Return whether another turn is needed, using only verified ledger state."""
        s = self.session
        after = self.measure()
        try:
            hint = json.loads(payload.decode('utf-8'))
            hint = hint.get('status') if isinstance(hint, dict) else None
        except (UnicodeError, ValueError):
            hint = None
        self.history.append({'turn':len(self.history)+1, 'work_scope':'whole_remaining_project',
                             'before':self.before, 'after':after, 'model_status':hint})
        advanced = (after['draft'] > self.before['draft'] or after['reviewed'] > self.before['reviewed']
                    or after['checks_pages'] > self.before['checks_pages']
                    or (after['checks_read'] and not self.before['checks_read']))
        self.stalled = 0 if advanced else self.stalled+1
        if s.phase == 'translating' and not self.remaining():
            s.begin_review()
            self.stalled = 0
        elif s.phase == 'reviewing' and not self.remaining() and s.checks_read:
            self._persist()
            return False
        self._persist()
        if self.stalled >= self.max_stalled_turns:
            raise JobError('PROJECT_NO_PROGRESS', '连续三轮没有新增翻译、审校或检查进度，已保留检查点。')
        if len(self.history) >= self.max_turns:
            raise JobError('PROJECT_TURN_LIMIT', '整单续轮次数达到上限，已保留检查点。')
        return True

    def _persist(self):
        write_json(self.session.root/'control/project-controller.json',
                   {'version':self.version, 'stalled_turns':self.stalled, 'turns':self.history})
