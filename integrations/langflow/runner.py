from lfx.custom import Component
from lfx.io import HandleInput, StrInput, SecretStrInput, DropdownInput, IntInput, BoolInput, Output
from lfx.schema import Message
from lfx.services.deps import get_storage_service, get_settings_service, session_scope
from lfx.utils.file_path_security import component_file_access_scopes, enforce_local_file_access


class RemoteCodexTranslation(Component):
    display_name = '02 远程 Codex · 自主翻译'
    description = '上传资料后直接运行：自动提交、续查或取回已有结果。重新翻译、取消和连接诊断在高级设置中；使用远端 Codex 现有配置。'
    name = 'RemoteCodexTranslation'
    icon = 'Server'
    inputs = [
        HandleInput(name='bundle', display_name='原始任务文件', input_types=['Data'], required=False),
        StrInput(name='service_url', display_name='Windows 服务地址', value='', required=True),
        SecretStrInput(name='service_token', display_name='服务访问令牌', required=False,
            info='填写 Windows Worker 的访问令牌，不是 OpenRouter Key；留空仅能检查网络。'),
        DropdownInput(name='action', display_name='高级操作', options=['自动运行', '重新翻译', '取消任务', '检查连接'],
            value='自动运行', advanced=True,
            info='通常保持自动运行。重新翻译会新建任务并再次调用 Codex；操作选项会保留，续查前请切回自动运行。'),
        IntInput(name='timeout_seconds', display_name='远端执行时限（秒）', value=1800, advanced=True),
        IntInput(name='wait_seconds', display_name='本次等待（秒）', value=900, advanced=True,
            info='等待结束会保留远端任务；再次运行即可继续查看和取回结果。'),
        StrInput(name='langflow_public_url', display_name='Langflow 对外地址（可选）', value='', advanced=True,
            info='留空使用同源下载路径；反向代理有路径前缀时请填写完整基址。'),
        BoolInput(name='allow_private_http', display_name='允许受保护私网 HTTP', value=False, advanced=True),
        BoolInput(name='allow_insecure_http', display_name='临时允许明文 HTTP', value=False, advanced=True,
            info='仅临时联调。开启后公网 HTTP 会明文传送服务令牌和文件；正式使用请改为 HTTPS 并关闭。'),
    ]
    outputs = [Output(name='result', display_name='译文、检查与 Trace', method='run_remote')]

    async def publish_file(self, path, filename):
        from fastapi import UploadFile
        from langflow.api.v2.files import upload_user_file
        from langflow.services.database.models.user.crud import get_user_by_id
        async with session_scope() as session:
            user = await get_user_by_id(session, self.user_id)
            with path.open('rb') as stream:
                result = await upload_user_file(file=UploadFile(filename=filename, file=stream, size=path.stat().st_size),
                    session=session, current_user=user, storage_service=get_storage_service(),
                    settings_service=get_settings_service(), append=False)
            return {'id': str(result.id), 'path': result.path}

    async def run_remote(self) -> Message:
        token = self.service_token.get_secret_value() if hasattr(self.service_token, 'get_secret_value') else str(self.service_token or '')
        client, reference, state_path, submit_lock = None, None, None, None
        action = {'自动运行': '提交或继续', '重新翻译': '重新试跑', '取消任务': '取消'}.get(self.action, self.action)
        try:
            client = RemoteClient(str(self.service_url), token, allow_private_http=bool(self.allow_private_http),
                                  allow_insecure_http=bool(self.allow_insecure_http))
            if action == '检查连接':
                check = await client.probe()
                lines = ['网络：已连通。']
                if check['authenticated']:
                    lines += ['服务令牌：鉴权通过。', '执行环境：' + ('已就绪。' if check['ready'] else '未就绪。')]
                    if check.get('codex_version'):
                        lines.append('Codex 版本：' + remote_text(check['codex_version'], token))
                else:
                    lines.append('鉴权和执行环境：未检查，请填写服务访问令牌后再检查。')
                if check['authenticated'] and check.get('message'):
                    lines.append(check['message'])
                if check['http_plaintext']:
                    lines.append('当前为 HTTP 明文连接；令牌和资料会明文传输。')
                lines.append('本次未上传资料、未创建翻译任务，也未调用模型。')
                self.status = '连接检查完成' if check['ready'] is not False else '网络已连通，执行环境未就绪'
                return Message(text='\n\n'.join(lines))
            query_only = action in {'查询', '取消'}
            if action not in {'提交或继续', '重新试跑', '查询', '取消'}:
                raise RemoteWorkerError('请选择有效的本次操作。')
            if not token:
                raise RemoteWorkerError('请先填写服务访问令牌，可使用“检查连接”确认鉴权。')
            if not self.user_id or not self.graph.flow_id:
                raise RemoteWorkerError('请在已保存的 Langflow 流程中运行此节点。')
            if not 60 <= int(self.timeout_seconds) <= 7200 or not 0 <= int(self.wait_seconds) <= 7200:
                raise RemoteWorkerError('执行时限须为 60–7200 秒，本次等待须为 0–7200 秒。')
            origin = str(self.langflow_public_url or '').rstrip('/')
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise RemoteWorkerError('Langflow 对外地址必须为无凭据的 HTTP(S) 基址。')
            scope = component_file_access_scopes(self)
            files = {}
            if not query_only:
                bundle = getattr(getattr(self, 'bundle', None), 'data', {})
                if bundle and (bundle.get('user_id') != str(self.user_id) or bundle.get('flow_id') != str(self.graph.flow_id)):
                    raise RemoteWorkerError('资料必须来自当前用户和当前流程的第一个节点。')
                files = {role: enforce_local_file_access(path, scope_ids=scope) for role, path in bundle.get('files', {}).items()}
                if files:
                    validate_upload_files(files)
            options = {'timeout_seconds': int(self.timeout_seconds)}
            identity = {'user_id': str(self.user_id), 'flow_id': str(self.graph.flow_id), 'node_id': str(self._id), 'service': client.base}
            identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            root = enforce_local_file_access(get_storage_service().build_full_path(str(self.user_id), 'codex_remote/' + identity_hash), scope_ids=scope)
            root.mkdir(parents=True, exist_ok=True)
            state_path = root / 'reference.json'
            previous = read_reference(state_path)
            if previous and previous.get('identity') != identity:
                raise RemoteWorkerError('保存的任务不属于当前用户、流程或服务地址。')
            if not files and not query_only and (not previous or action == '重新试跑'):
                raise RemoteWorkerError('首次运行或重新翻译需要四份有效资料，请在第一个节点上传。')
            fingerprint = previous.get('fingerprint', '') if previous else ''
            if files:
                fingerprint = hashlib.sha256(json.dumps({'options': options,
                    'files': {role: {'name': Path(path).name, 'sha256': await asyncio.to_thread(local_hash, path)} for role, path in files.items()}},
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cached_job = None
            if previous and previous.get('job_id') and action != '取消':
                cached_job = await client.status(previous['job_id'])
                if not update_reference(state_path, identity, previous['request_key'], status=cached_job['status']):
                    raise RemoteWorkerError('另一请求已更新当前任务，请再次运行以读取最新记录。')
            reference = reserve_reference(state_path, identity, fingerprint, action)
            submitted_now = False
            if not reference.get('job_id'):
                if query_only or not files:
                    if reference.get('status') == 'failed':
                        return Message(text='上次提交尚未创建远端任务。修正令牌、资料或服务配置，提供四份资料后再次运行。')
                    return Message(text='上次提交尚未收到任务编号。请恢复相同的四份资料和参数，再次运行可按原提交编号找回任务；当前不能确认已取消。')
                submit_lock = ReferenceLock(root / 'submit.lock')
                current = read_reference(state_path)
                if current.get('request_key') != reference['request_key']:
                    raise RemoteWorkerError('当前提交记录已变化，请再次运行。')
                attempted_before = current.get('submission_attempted', True)
                # Missing markers on old references are treated as uncertain submissions.
                if not attempted_before:
                    self.status = '正在检查连接与执行环境'
                    try:
                        check = await client.probe()
                        if check['ready'] is not True:
                            raise RemoteWorkerError('执行环境尚未就绪：' + (check.get('message') or '请检查 Windows Worker 自检结果。'))
                    except (RemoteWorkerError, httpx.TransportError, asyncio.CancelledError):
                        update_reference(state_path, identity, reference['request_key'], status='failed', only_unsubmitted=True)
                        raise
                self.status = '正在提交原始资料'
                if not update_reference(state_path, identity, reference['request_key'], submission_attempted=True):
                    raise RemoteWorkerError('当前提交记录已变化，请再次运行。')
                try:
                    submitted = await client.submit(files, options, reference['request_key'])
                except RemoteWorkerError as exc:
                    if exc.rejected and not attempted_before:
                        update_reference(state_path, identity, reference['request_key'], status='failed', submission_attempted=False, only_unsubmitted=True)
                    elif exc.rejected:
                        exc.rejected = False
                    raise
                submitted_now = not submitted.get('reused', False)
                reference.update(job_id=submitted['job_id'], status=submitted['status'])
                update_reference(state_path, identity, reference['request_key'], job_id=submitted['job_id'], status=submitted['status'])
                submit_lock.close()
                submit_lock = None
            job_id = reference['job_id']
            if action == '取消':
                await client.cancel(job_id)
                cached_job = None
            deadline = asyncio.get_running_loop().time() + (0 if query_only else int(self.wait_seconds))
            while True:
                job = cached_job if cached_job and cached_job['job_id'] == job_id else await client.status(job_id)
                cached_job = None
                update_reference(state_path, identity, reference['request_key'], status=job['status'])
                self.status = f"{job_id}：{job['status']}"
                if job['status'] in REMOTE_TERMINAL or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(3)
            labels = {'completed': '文件已生成，程序检查通过', 'needs_review': '文件已生成，需复核',
                      'failed': '任务失败', 'cancelled': '已取消', 'interrupted': '任务中断',
                      'queued': '排队中', 'running': '执行中', 'checking': '检查中', 'cancelling': '取消处理中，尚未确认停止'}
            links, attachments = [], []
            for entry in job.get('artifacts', []):
                path = await client.download(job_id, entry, root / job_id)
                saved = reference.setdefault('published', {}).get(entry['id'])
                if saved:
                    existing = enforce_local_file_access(self.get_full_path(saved['path']), scope_ids=scope)
                    if not existing.is_file() or await asyncio.to_thread(local_hash, existing) != entry['sha256']:
                        saved = None
                if saved is None:
                    saved = await self.publish_file(path, entry['filename'])
                    reference['published'][entry['id']] = saved
                    update_reference(state_path, identity, reference['request_key'], published={entry['id']: saved})
                url = origin + '/api/v2/files/' + saved['id']
                label = entry['filename'].replace('[', '\\[').replace(']', '\\]')
                links.append(f"[{label}]({url})")
                attachments.append(str(self.get_full_path(saved['path'])))
            error = job.get('error') or {}
            message = f"{labels[job['status']]}。任务编号：`{job_id}`。"
            if not submitted_now and action != '取消':
                if job['status'] in {'completed', 'needs_review'}:
                    message += '\n\n已取回已有任务的结果，本次未重新翻译。'
                elif job['status'] in REMOTE_TERMINAL:
                    message += '\n\n这是已有任务的状态，本次未重新翻译。确认需要重跑时，在高级设置选择“重新翻译”。'
                else:
                    message += '\n\n正在继续已有任务，本次未重复提交。'
            if action == '重新试跑':
                message += '\n\n续查请将高级操作切回“自动运行”；保留“重新翻译”会在任务结束后再次新建任务。'
            if error.get('message'):
                message += '\n\n' + remote_text(error['message'], token)
            if job['status'] not in REMOTE_TERMINAL:
                message += '\n\n任务引用已保存。在自动运行模式下再次点击运行即可续查；本次等待结束不会自动取消远端任务。'
            if links:
                message += '\n\n' + '\n\n'.join(links)
            if job['status'] in {'completed', 'needs_review'}:
                message += '\n\n程序检查不代表语言质量已通过独立审校。'
            return Message(text=message, files=attachments)
        except asyncio.CancelledError:
            if reference and reference.get('job_id'):
                async def request_cancel():
                    try:
                        await client.cancel(reference['job_id'])
                        job = await client.status(reference['job_id'])
                        update_reference(state_path, identity, reference['request_key'], status=job['status'])
                    except Exception:
                        update_reference(state_path, identity, reference['request_key'], cancel_unconfirmed=True)
                try:
                    await asyncio.shield(asyncio.wait_for(request_cancel(), 10))
                except (Exception, asyncio.CancelledError):
                    pass
            raise
        except (RemoteWorkerError, httpx.TransportError) as exc:
            message = remote_text(str(exc), token)
            self.status = '连接或操作未完成'
            if reference:
                job_id = reference.get('job_id')
                if not job_id and getattr(exc, 'rejected', False):
                    message += '\n\n提交被拒绝，未取得任务号。修正后再次运行即可重试。'
                else:
                    message += f"\n\n任务编号：`{job_id or '尚未取得'}`。任务引用已保留，请保持资料和参数一致，在自动运行模式下再次运行。"
            return Message(text=message)
        finally:
            if submit_lock:
                submit_lock.close()
            if client:
                await client.close()
