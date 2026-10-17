"""Download what the site needs from the private HF repos (nothing comes from the laptop)."""
import os, shutil, glob, time
from pathlib import Path
from huggingface_hub import hf_hub_download, snapshot_download

TOKEN, REPO, DATA_REPO = os.environ["HF_TOKEN"], "invinciblefire/arety-rcb", "invinciblefire/arety-site-data"
H = Path("/home/user")
t0 = time.time()
snapshot_download(REPO, local_dir=H / "rcb", token=TOKEN, ignore_patterns=["outputs/*", "control/*", "notebooks/*", "kaggle/*"])
k = H / "knowledge"
shutil.rmtree(k, ignore_errors=True)
shutil.copytree(H / "rcb" / "knowledge", k)
snapshot_download(REPO, local_dir=H / "tmp-teachers", token=TOKEN, allow_patterns=["outputs/teachers/information/*.json"])
(k / "information").mkdir(exist_ok=True)
for f in glob.glob(str(H / "tmp-teachers/outputs/teachers/information/*.json")):
    shutil.copy(f, k / "information" / ("teachers-" + os.path.basename(f)))
db = H / "db"
db.mkdir(exist_ok=True)
for path, name in [("outputs/dictionary/dictionary.sqlite", "dictionary.sqlite"), ("outputs/wiki/encyclopedia.sqlite", "encyclopedia.sqlite"),
                   ("outputs/wikidata/wikidata.sqlite", "wikidata.sqlite")]:
    p = hf_hub_download(REPO, path, token=TOKEN, local_dir=H / "dl")
    os.replace(p, db / name)
    print(f"{name}: {(db / name).stat().st_size / 1e9:.1f} GB ({time.time() - t0:.0f} s)", flush=True)
shutil.rmtree(H / "dl", ignore_errors=True)
# accounts, chats and per-account memory
data = Path(os.environ["SITE_DATA"])
try:
    snapshot_download(DATA_REPO, repo_type="dataset", local_dir=data, token=TOKEN)
except Exception as e:  # noqa: BLE001  (first start: nothing saved yet)
    print("no saved site data:", e, flush=True)
if (data / "arety-rcb-1.0").is_dir() and not (data / "arety-rcb-1.2").exists():
    shutil.copytree(data / "arety-rcb-1.0", data / "arety-rcb-1.2")     # 1.2 starts with what 1.0 remembered
print(f"ready in {time.time() - t0:.0f} s", flush=True)
