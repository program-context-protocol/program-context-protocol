import subprocess, sys
sys.path.insert(0,"src")
from pathlib import Path
from pcp.commands.build import _seed_testmon_cache

def _repo(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git","init","-q"],cwd=p,check=True)
    subprocess.run(["git","config","user.email","t@t"],cwd=p,check=True)
    subprocess.run(["git","config","user.name","t"],cwd=p,check=True)
    (p/".gitignore").write_text(".testmondata\n")
    (p/"f.py").write_text("x=1\n")
    subprocess.run(["git","add","-A"],cwd=p,check=True)
    subprocess.run(["git","commit","-qm","i"],cwd=p,check=True)
    return p

def test_seeds_gitignored_testmon_cache_into_worktree(tmp_path):
    root=_repo(tmp_path/"r"); (root/".testmondata").write_bytes(b"COVDB")
    wt=tmp_path/"wt"
    subprocess.run(["git","worktree","add","-q","--detach",str(wt),"HEAD"],cwd=root,check=True)
    assert not (wt/".testmondata").exists(), "git carried a gitignored file — premise broken"
    _seed_testmon_cache(root, wt)
    assert (wt/".testmondata").read_bytes()==b"COVDB"

def test_seed_never_overwrites_a_warm_worktree_cache(tmp_path):
    root=_repo(tmp_path/"r"); (root/".testmondata").write_bytes(b"BASE")
    wt=tmp_path/"wt"; wt.mkdir(); (wt/".testmondata").write_bytes(b"WARMER")
    _seed_testmon_cache(root, wt)
    assert (wt/".testmondata").read_bytes()==b"WARMER"

def test_seed_is_a_noop_without_a_source_cache(tmp_path):
    root=_repo(tmp_path/"r"); wt=tmp_path/"wt"; wt.mkdir()
    _seed_testmon_cache(root, wt)
    assert not (wt/".testmondata").exists()

def test_seed_never_raises_when_destination_is_unwritable(tmp_path):
    root=_repo(tmp_path/"r"); (root/".testmondata").write_bytes(b"X")
    _seed_testmon_cache(root, tmp_path/"does"/"not"/"exist")
