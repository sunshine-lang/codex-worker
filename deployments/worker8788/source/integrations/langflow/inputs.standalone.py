from pathlib import Path
from lfx.custom import Component
from lfx.io import FileInput, Output
from lfx.schema import Data
from lfx.utils.file_path_security import component_file_access_scopes, enforce_local_file_access


class RemoteTranslationInputs(Component):
    display_name = '01 上传原表与翻译资料'
    description = '四份资料将发送至配置的 Windows Codex 服务。'
    name = 'RemoteTranslationInputs'
    icon = 'Files'
    inputs = [
        FileInput(name='source_file', display_name='待译文件', file_types=['xlsx', 'csv'], info='首次提交必填；续查或取消已有任务可留空。'),
        FileInput(name='prompt_file', display_name='翻译 Prompt', file_types=['md', 'txt'], info='首次提交必填；续查或取消已有任务可留空。'),
        FileInput(name='project_style', display_name='西门子风格指南', file_types=['md', 'txt', 'docx', 'doc']),
        FileInput(name='general_style', display_name='EC 通用准则', file_types=['md', 'txt', 'docx', 'doc']),
    ]
    outputs = [Output(name='bundle', display_name='原始任务文件', method='bundle_files')]

    def bundle_files(self) -> Data:
        if not self.user_id:
            raise ValueError('请登录 Langflow 后上传任务资料。')
        files = {}
        for name in ['source_file', 'prompt_file', 'project_style', 'general_style']:
            value = getattr(self, name)
            if not value:
                continue
            if isinstance(value, list):
                if len(value) != 1:
                    raise ValueError('每个上传框需要一份文件。')
                value = value[0]
            if not isinstance(value, str) or not value:
                raise ValueError('请上传四份任务资料。')
            path = value if Path(value).is_absolute() else self.get_full_path(value)
            path = enforce_local_file_access(path, scope_ids=component_file_access_scopes(self))
            if not path.is_file():
                labels = {'source_file': '待译文件', 'prompt_file': '翻译 Prompt',
                          'project_style': '项目风格指南', 'general_style': '通用准则'}
                raise ValueError(f'「{labels[name]}」的文件在当前 Langflow 不存在，请重新上传或清空此绑定。')
            files[name] = str(path)
        return Data(data={'flow_id': str(self.graph.flow_id), 'user_id': str(self.user_id), 'files': files})
