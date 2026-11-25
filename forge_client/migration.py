"""Schema upgrade CLI: a resumable maintenance window on an installation that already uses SQLite."""
import base64
import io
from importlib.resources import files
import json
import re
from pathlib import Path
import shlex
import subprocess
import zipfile

from .bootstrap import Host
from .contracts import SetupError, require

# This bootstrap reads only locally supplied code and intent. The remote host
# never supplies commands. Temporary tools do not replace the running checkout.
BOOTSTRAP = r'''import base64,hashlib,json,pathlib,sys,tempfile,zipfile,io,os,subprocess
payload=sys.stdin.buffer.read(64*1024*1024+1)
if len(payload)>64*1024*1024: raise ValueError("request too large")
request=json.loads(payload)
release_access=request.pop("release_access",None)
raw=base64.b64decode(request.pop("bundle"),validate=True)
if hashlib.sha256(raw).hexdigest()!=request.pop("bundle_digest"): raise ValueError("bundle mismatch")
with tempfile.TemporaryDirectory(prefix="forge-migration-tool-") as directory:
 with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
  for name in bundle.namelist():
   p=pathlib.PurePosixPath(name)
   if p.is_absolute() or ".." in p.parts or (p.parts[0]!="forge" and name not in ("forge_client/__init__.py","forge_client/contracts.py")): raise ValueError("bad bundle")
  bundle.extractall(directory)
 sys.path.insert(0,directory)
 from forge.store.migration.host import run,failure
 from forge.store.migration.snapshot import MigrationError
 from forge.store.db import StoreError,qualified_sqlite
 try:
  if release_access is not None:
   if request.get("apply") is not True or not isinstance(release_access,dict) or release_access.get("revision")!=request.get("source_revision"): raise MigrationError("invalid_release_access")
   from forge.release_artifacts import stage,source_build,ArtifactError
   try: source_build() or stage(release_access)
   except ArtifactError: raise MigrationError("prepared_release_unavailable") from None
   release_access=None
  prepared=None
  if not qualified_sqlite():
   from forge.store.migration.runtime import inspect
   prepared=inspect(request["root"])
  if prepared:
   child="import json,sys;sys.path.insert(0,"+repr(directory)+");from forge.store.migration.host import run,failure;request=json.load(sys.stdin)\ntry: print(json.dumps({'ok':True,**run(request)}))\nexcept Exception as e: print(json.dumps(failure(e,request)))"
   completed=subprocess.run([sys.executable,"-I","-c",child],input=json.dumps(request),capture_output=True,text=True,timeout=1450,env={**os.environ,"LD_LIBRARY_PATH":prepared["library_directory"]})
   if completed.returncode or len(completed.stdout)>65536: raise MigrationError("qualified_migration_failed")
   print(completed.stdout.strip())
  else: print(json.dumps({"ok":True,**run(request)}))
 except Exception as error: print(json.dumps(failure(error,request)))
'''


def bundle():
    import forge
    from forge.store.migration.source import migration_files
    root=Path(forge.__file__).parent.parent
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in migration_files(root):
            archive.writestr(path.relative_to(root).as_posix(),path.read_bytes())
    return output.getvalue()


def ssh(host, command):
    return ['ssh','-T','-a','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15',
            '-o','ClearAllForwardings=yes','-o','ForwardX11=no',host,command]


SERVICE_USER_PATTERN = r'[a-z_][a-z0-9_-]{0,31}'


def service_user(args):
    user = getattr(args, 'service_user', None)
    require(user is None or isinstance(user, str) and re.fullmatch(SERVICE_USER_PATTERN, user),
            'invalid_service_user', 'Use the existing Unix service account name, such as forge.')
    return user


def transport(args, command):
    """Maintenance RPCs run as the explicitly selected account."""
    user = service_user(args)
    if user is not None:
        program = files('forge_client').joinpath('_service_host.py').read_text()
        command = ['python3', '-I', '-c', program, user, *command]
    return ssh(args.ssh, shlex.join(command))


def call(args,action,**fields):
    # Reuse validation of explicit host/source/root intent.
    Host(args.ssh,args.source,args.root)
    user = service_user(args)
    resume = shlex.join(['forge', 'migrate', action, '--ssh', args.ssh,
                         '--source', args.source, '--root', args.root,
                         *(['--service-user', user] if user is not None else []), '--json'])
    if getattr(args,'source_revision',None):
        resume += ' --apply --source-revision '+shlex.quote(args.source_revision)
    from forge.store.migration.snapshot import digest
    code=bundle()
    request=dict(action=action,root=args.root,source=args.source,bundle=base64.b64encode(code).decode(),bundle_digest=digest(code),**fields)
    try:
        result=subprocess.run(transport(args, ['python3', '-c', BOOTSTRAP]),input=json.dumps(request).encode(),capture_output=True,timeout=4500)
    except subprocess.TimeoutExpired:
        raise SetupError('migration_transport_timeout',
                         'The SSH maintenance call timed out. The host operation may still be running or may have completed. '
                         'Resume maintenance with: ' + resume) from None
    require(result.returncode!=255,'migration_transport_failed',
            'The SSH maintenance session failed. Check host connectivity and SSH access. '
            'The host operation may still be running or may have completed. Resume maintenance with: ' + resume)
    require(result.returncode==0 and len(result.stdout)<=65536,'migration_host_failed',
            ('The host operation failed. ' if user is None else 'Maintenance could not run as service user '+user+'. Verify that account and root or noninteractive sudo access. ')
            + 'Resume maintenance with: ' + resume)
    value=json.loads(result.stdout)
    if value.get('ok') is not True:
        token=lambda key,default: value[key] if isinstance(value.get(key),str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',value[key]) else default
        code,kind,stage=token('error','failed'),token('exception_type','Exception'),token('stage','unknown')
        if code=='drain_configuration_changed':
            raise SetupError('migration_'+code,'Runtime settings changed while maintenance was draining. The CLI kept the newer settings. '
                'Switch off agents explicitly before continuing, then resume with: '+resume)
        detail = value.get('detail')
        detail = ('\nHost output (last lines):\n' + detail[-4000:]) if isinstance(detail, str) and detail.strip() else ''
        raise SetupError('migration_'+code,'Schema upgrade could not proceed: '+code+' ('+kind+' at '+stage+'). Resume with: '+resume+detail)

    if value.get('next_command') == 'forge migrate upgrade':
        value['next_command'] = resume
    return value


def execute(args):
    from forge.store.migration.snapshot import MigrationError
    from forge.store.db import StoreError
    try:
        service_user(args)
        revision=getattr(args,'source_revision',None)
        require(args.ssh and args.source and args.root,'migration_host_required','Select --ssh, --source and --root for this host.')
        require(not args.apply or isinstance(revision,str) and re.fullmatch(r'[0-9a-f]{40}',revision),'upgrade_revision_required','Supply the full release SHA with --source-revision when using --apply.')
        if not args.apply:
            return call(args,'upgrade',apply=False,source_revision=revision)
        observed = call(args,'upgrade',apply=False,source_revision=revision)
        previous = observed.get('source_revision')
        require(isinstance(previous,str) and re.fullmatch(r'[0-9a-f]{40}',previous),
                'invalid_host_response','The host did not report its installed source revision.')
        # Both source and private release assets are authorized locally. Even
        # after checkout, resumption may still need to fill the host asset cache.
        fields = {}
        if previous != revision:
            from .source import export
            fields['source_pack'] = export(revision, previous=previous)
        from .release_delivery import access
        fields['release_access'] = access(revision)
        return call(args,'upgrade',apply=True,source_revision=revision,**fields)

    except (MigrationError, StoreError) as error:
        raise SetupError('migration_'+error.code,'Schema upgrade could not proceed: '+error.code) from None
