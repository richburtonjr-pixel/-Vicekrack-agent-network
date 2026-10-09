"""Location-only credential audit: never prints matching contents or secret values."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = [rb"xai-[A-Za-z0-9_-]{24,}", rb"sk-(?:ant-)?[A-Za-z0-9_-]{20,}", rb"gh[pousr]_[A-Za-z0-9]{30,}",
            rb"github_pat_[A-Za-z0-9_]{30,}", rb"AKIA[0-9A-Z]{16}",
            rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"]

def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)

def scan(data, location):
    return [{**location, "line": number} for number, line in enumerate(data.splitlines(), 1)
            if any(re.search(pattern, line) for pattern in PATTERNS)]

def main():
    hits = []
    blobs = 0
    for row in git("cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)").decode().splitlines():
        oid, kind = row.split()
        if kind == "blob":
            blobs += 1
            hits.extend(scan(git("cat-file", "blob", oid), {"blob": oid}))
    tracked = git("ls-files", "-z").decode().split("\0")
    env_paths = set()
    for name in filter(None, tracked):
        path = ROOT / name
        if path.is_file():
            hits.extend(scan(path.read_bytes(), {"path": name}))
        if Path(name).name.startswith(".env") and Path(name).name != ".env.example":
            env_paths.add(name)
    # Inspect every accessible commit, including reflog commits and deleted paths.
    for commit in git("rev-list", "--all", "--reflog").decode().splitlines():
        for name in git("ls-tree", "-r", "--name-only", commit).decode().splitlines():
            if Path(name).name.startswith(".env") and Path(name).name != ".env.example":
                env_paths.add(name)
    print(json.dumps({"local_git_blobs_scanned": blobs, "candidate_locations": hits,
                      "committed_environment_paths": sorted(env_paths)}, indent=2))
    return 1 if hits or env_paths else 0

if __name__ == "__main__":
    raise SystemExit(main())
