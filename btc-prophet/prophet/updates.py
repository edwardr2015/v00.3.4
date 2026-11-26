"""Validate a local release and prepare an isolated copy after owner approval."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
import tomllib
from zipfile import BadZipFile, ZipFile

from .operations import journal

MAX_ARCHIVE = 5*2**20
MAX_EXPANDED = 20*2**20
ALLOWED_FILES = {"README.md","pyproject.toml","Start Prophet.command",".gitignore","MANIFEST.json"}
ALLOWED_FOLDERS = {"prophet","scripts","tests","docs"}


def verify(data,expected):
    if len(data)>MAX_ARCHIVE or not re.fullmatch(r"[a-fA-F0-9]{64}",expected or ""):
        raise ValueError("Provide a ZIP under 5 MB and its trusted SHA-256 checksum.")
    digest=sha256(data).hexdigest()
    if digest!=expected.lower():
        raise ValueError("The ZIP checksum does not match.")
    try:
        with ZipFile(BytesIO(data)) as archive:
            if sum(entry.file_size for entry in archive.infolist())>MAX_EXPANDED:
                raise ValueError("Expanded package exceeds 20 MB.")
            files={}
            root=None
            for entry in archive.infolist():
                path=PurePosixPath(entry.filename)
                if path.is_absolute() or len(path.parts)<2 or ".." in path.parts or "\\" in entry.filename:
                    raise ValueError("Invalid archive path.")
                root=root or path.parts[0]
                if root!=path.parts[0] or not root.startswith("ai-btc-prophet-"):
                    raise ValueError("Expected one BTC Prophet release folder.")
                relative=PurePosixPath(*path.parts[1:]).as_posix()
                if relative in files or (entry.external_attr>>16)&0o170000==0o120000:
                    raise ValueError("Duplicate file or symlink in update.")
                if relative not in ALLOWED_FILES and path.parts[1] not in ALLOWED_FOLDERS:
                    raise ValueError("Unexpected file category.")
                if any(part in {"data","research","__pycache__",".env",".git"} for part in path.parts):
                    raise ValueError("Private data and caches are excluded from updates.")
                if not entry.is_dir():
                    files[relative]=archive.read(entry)
            manifest=json.loads(files["MANIFEST.json"])
            if len(manifest)!=len(files)-1:
                raise ValueError("Manifest file count differs.")
            seen=set()
            for item in manifest:
                name=item["path"]
                if name in seen or name=="MANIFEST.json":
                    raise ValueError("Duplicate file in manifest.")
                seen.add(name)
                if sha256(files[name]).hexdigest()!=item["sha256"] or len(files[name])!=item["bytes"]:
                    raise ValueError("Manifest file checksum differs.")
            settings=tomllib.loads(files["pyproject.toml"].decode())["project"]
            if settings["name"]!="ai-btc-prophet" or "Start Prophet.command" not in files:
                raise ValueError("Expected the BTC Prophet project and launcher.")
    except (BadZipFile,KeyError,TypeError,RuntimeError,UnicodeError) as exc:
        raise ValueError("Incomplete or unreadable release ZIP.") from exc
    return dict(root=root,version=settings["version"],sha256=digest,files=len(files)),files


def inbox(store):
    return Path(store.path).parent/"updates"


def test_update(store,data,checksum):
    descriptor,_=verify(data,checksum)
    folder=inbox(store)
    folder.mkdir(mode=0o700,exist_ok=True)
    target=folder/(descriptor["sha256"]+".zip")
    with tempfile.NamedTemporaryFile(dir=folder,delete=False) as file:
        file.write(data)
        temporary=Path(file.name)
    temporary.replace(target)
    with store.connect() as db:
        journal(db,time.time(),"test-update","VERIFIED",descriptor)
    return descriptor


def stage(store,digest):
    if not re.fullmatch(r"[a-f0-9]{64}",digest):
        raise ValueError("Choose a previously tested update.")
    folder=inbox(store)
    descriptor,files=verify((folder/(digest+".zip")).read_bytes(),digest)
    destination=folder/(descriptor["root"]+"-"+digest[:8])
    backup=folder/("before-"+digest[:8]+".sqlite3")
    with store.connect() as db:
        prior=db.execute("SELECT 1 FROM repair_history WHERE action='stage-update' AND details LIKE ?",
                         ("%"+digest+"%",)).fetchone()
    if destination.exists() or prior:
        raise ValueError("This update is already staged.")
    # Never use a link or write back into the currently running source tree.
    # Use a unique backup filename so prior verified ledgers remain intact.
    if backup.exists():
        backup=folder/("before-"+digest[:8]+"-"+str(time.time_ns())+".sqlite3")
    store.backup(str(backup))
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        extracted=Path(temp)/"source"
        for name,data in files.items():
            file=extracted/name
            file.parent.mkdir(parents=True,exist_ok=True)
            file.write_bytes(data)
            if name=="Start Prophet.command":
                file.chmod(0o700)
        extracted.rename(destination)
    receipt=dict(descriptor,directory=destination.name,backup=backup.name)
    with store.connect() as db:
        journal(db,time.time(),"stage-update","STAGED",receipt)
    return receipt


def select_release(store,digest=None):
    """Schedule a verified code switch or rollback. The same ledger opens after restart."""
    pointer=inbox(store)/"next-release.json"
    pointer.parent.mkdir(mode=0o700,exist_ok=True)
    selection=None
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if digest is not None:
            if not re.fullmatch(r"[a-f0-9]{64}",digest):
                raise ValueError("Choose a staged update.")
            for row in db.execute("SELECT details FROM repair_history WHERE action='stage-update' ORDER BY id DESC"):
                descriptor=json.loads(row[0])
                if descriptor.get("sha256")==digest:
                    selection=descriptor
                    break
            if selection is None:
                raise ValueError("Back up and stage the tested update first.")
            folder=inbox(store)/selection["directory"]
            _,files=verify((inbox(store)/(digest+".zip")).read_bytes(),digest)
            if any(not (folder/name).is_file() or (folder/name).read_bytes()!=data for name,data in files.items()):
                raise ValueError("The staged files differ from the verified package.")
        with tempfile.NamedTemporaryFile(dir=pointer.parent,delete=False,mode="w") as file:
            json.dump(selection,file)
            temporary=Path(file.name)
        temporary.replace(pointer)
        journal(db,time.time(),"activate-update" if digest else "rollback-update","READY_FOR_RESTART",
                 dict(selection=selection,ledger_preserved=True))
    return dict(selection=selection,restart_required=True)
