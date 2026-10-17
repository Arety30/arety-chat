"""Save accounts, chats and per-account memory to the private dataset repo every 10 minutes (when changed)."""
import os, shutil, sqlite3, time, signal, sys
from pathlib import Path
from huggingface_hub import HfApi

api = HfApi(token=os.environ["HF_TOKEN"])
REPO, DATA = "invinciblefire/arety-site-data", Path(os.environ["SITE_DATA"])
STAGE = Path("/tmp/site-stage")
SKIP = {".cache", ".huggingface", ".gitattributes"}


def snapshot() -> float:
    newest = 0.0
    shutil.rmtree(STAGE, ignore_errors=True)
    for p in DATA.rglob("*"):
        rel = p.relative_to(DATA)
        if rel.parts[0] in SKIP or not p.is_file() or p.name.endswith(("-wal", "-shm", "-journal")):
            continue
        wal = Path(str(p) + "-wal")
        newest = max(newest, p.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0.0)
        out = STAGE / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix == ".sqlite":                       # consistent copy of a live database
            src, dst = sqlite3.connect(p), sqlite3.connect(out)
            src.backup(dst)
            src.close(), dst.close()
        else:
            shutil.copy2(p, out)
    return newest


def save(last: float) -> float:
    newest = snapshot()
    if newest > last:
        api.upload_folder(folder_path=str(STAGE), repo_id=REPO, repo_type="dataset", commit_message="site data")
        if len(api.list_repo_commits(REPO, repo_type="dataset")) > 100:
            api.super_squash_history(REPO, repo_type="dataset")
    return max(newest, last)


last = time.time()
signal.signal(signal.SIGTERM, lambda *_: (save(0), sys.exit(0)))
while True:
    time.sleep(600)
    try:
        last = save(last)
    except Exception as e:  # noqa: BLE001
        print("persist:", e, flush=True)
