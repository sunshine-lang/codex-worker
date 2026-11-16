"""Build a bounded-memory, self-contained Windows setup overlay."""
from pathlib import Path
import argparse
import hashlib
import copy
import time
import json
import os
import shutil
import struct
import zipfile

MAGIC = b'CRWSETUP20261109'
FORBIDDEN = {'credentials.local.json','service-account.local.json','auth.json','config.local.json','service-token.txt','a.txt',
             'bootstrap-credentials.local.json','install-plan.json','desktop-status.json','desktop.settings.json',
             'preflight.json','jobs.sqlite3','http-access.jsonl','worker.local.process.json'}
parser = argparse.ArgumentParser()
parser.add_argument('--stage',type=Path,required=True)
parser.add_argument('--stub',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--reuse-archive',action='store_true')
args = parser.parse_args()
stage = args.stage.resolve()
files = sorted(p for p in stage.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix.lower() not in {'.pyc','.pyo'} and p.name != '.DS_Store')
for path in files:
    relative = path.relative_to(stage)
    if (path.is_symlink() or any(part.lower() in {'private','.codex','.aws','jobs'} for part in relative.parts)
            or tuple(part.lower() for part in relative.parts[:2]) == ('app', 'data') or path.name.lower() in FORBIDDEN):
        raise SystemExit('Refusing to package sensitive or linked path: '+str(relative))
archive = args.output.with_suffix('.payload.zip')
args.output.parent.mkdir(parents=True,exist_ok=True)
if args.reuse_archive and archive.exists():
    replacement=archive.with_suffix('.new.zip')
    with zipfile.ZipFile(archive,'r') as previous, zipfile.ZipFile(replacement,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as package:
        entries=sorted(previous.infolist(),key=lambda item:item.header_offset)
        next_offsets={entry.filename:(entries[index+1].header_offset if index+1<len(entries) else previous.start_dir) for index,entry in enumerate(entries)}
        copied=0
        changed=0
        for path in files:
            name=path.relative_to(stage).as_posix()
            info=previous.NameToInfo.get(name)
            stat=path.stat()
            stamp=time.localtime(stat.st_mtime)[:6]
            same=info is not None and info.file_size==stat.st_size and stamp[:5]==info.date_time[:5] and stamp[5]//2==info.date_time[5]//2
            if same:
                duplicate=copy.copy(info)
                duplicate.header_offset=package.fp.tell()
                previous.fp.seek(info.header_offset)
                remaining=next_offsets[name]-info.header_offset
                while remaining:
                    chunk=previous.fp.read(min(1024*1024,remaining))
                    if not chunk:raise RuntimeError('Truncated build cache')
                    package.fp.write(chunk)
                    remaining-=len(chunk)
                package.filelist.append(duplicate)
                package.NameToInfo[name]=duplicate
                package.start_dir=package.fp.tell()
                package._didModify=True
                copied+=1
            else:
                package.write(path,name)
                changed+=1
        print(json.dumps({'phase':'update','reused_files':copied,'updated_files':changed}),flush=True)
    os.replace(replacement,archive)
else:
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as package:
        for index,path in enumerate(files,1):
            package.write(path,path.relative_to(stage).as_posix())
            if index%2000==0:
                print(json.dumps({'phase':'compress','files':index,'total':len(files)}),flush=True)
hash_value=hashlib.sha256()
with args.output.open('wb') as output:
    with args.stub.open('rb') as source: shutil.copyfileobj(source,output,1024*1024)
    offset=output.tell()
    with archive.open('rb') as source:
        while chunk:=source.read(1024*1024):
            hash_value.update(chunk)
            output.write(chunk)
    length=output.tell()-offset
    output.write(MAGIC+struct.pack('<qq',offset,length)+hash_value.digest())
    output.flush()
    os.fsync(output.fileno())
summary={'file':str(args.output.resolve()),'files':len(files),'payload_bytes':length,'installer_bytes':args.output.stat().st_size,'payload_sha256':hash_value.hexdigest(),'max_relative_path':max(len(p.relative_to(stage).as_posix()) for p in files)}
args.output.with_suffix('.manifest.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(json.dumps(summary),flush=True)

