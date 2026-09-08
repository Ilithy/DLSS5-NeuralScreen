"""Build the NeuralScreen v1.3.0 release archive: git files + artifacts + runtime."""
import os
import subprocess
import zipfile
from pathlib import Path

# The script must work from any directory: every path is relative to git.
BASE = Path(__file__).resolve().parent
os.chdir(BASE)

files = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
extra = [
    "NeuralScreen.exe",
    "NeuralScreen.vbs",
    "README.ru.md",
    "native/nvngx.dll",
    "native/nvngx_dlssnr.dll",
]
# tcl/tk stays out of the archive: the tkinter settings window is gone and the
# whole interface lives in the overlay menu. Nothing in the project imports
# tkinter (PIL/_tkinter_finder pulls it lazily and only for ImageTk).
TK_SKIP = ("runtime/tcl/", "runtime/tcl86t.dll", "runtime/tk86t.dll",
           "runtime/_tkinter.pyd", "runtime/Lib/tkinter/")

# The runtime was assembled for other work and drags in packages the program
# never touches: a web frontend, dataframes, a bundler. Checked through
# sys.modules after importing every project module — only av, cv2, numpy, PIL,
# pygame, pystray, dxcam and comtypes are needed. The rest is cut, ~130 MB
# before compression.
DROP_PACKAGES = {
    # gradio and its surroundings
    "gradio", "gradio_client", "hf_gradio", "huggingface_hub", "hf_xet",
    "fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core",
    "annotated_types", "annotated_doc", "typing_inspection",
    "safehttpx", "groovy", "pydub", "python_multipart", "multipart",
    "orjson", "httpx", "httpcore", "h11", "anyio", "idna", "certifi",
    "fsspec", "filelock", "jinja2", "markupsafe", "tqdm", "audioop",
    "audioop_lts", "brotli", "_brotli", "yaml", "_yaml", "pyyaml",
    "semantic_version", "tomlkit", "typer", "click", "shellingham",
    "rich", "markdown_it", "markdown_it_py", "mdurl", "pygments",
    # dataframes and time zones
    "pandas", "pytz", "tzdata", "dateutil", "python_dateutil",
    # bundler
    "PyInstaller", "pyinstaller", "_pyinstaller_hooks_contrib",
    "pyinstaller_hooks_contrib", "altgraph", "pefile", "peutils", "ordlookup",
    "psutil",
    # the end user has no use for a package manager
    "pip",
    # setuptools and its distutils shim: nothing in the project imports
    # pkg_resources or setuptools (checked with grep over every module).
    # ~2.8 MB of dead weight in the archive (audit #3, N2).
    "setuptools", "pkg_resources", "_distutils_hack", "distutils-precedence.pth",
}
SP = "runtime/Lib/site-packages/"

# Dev-only files that must NOT reach the release archive (user rule
# 2026-09-08: the archive contains only what the program needs to run).
DEV_ONLY = {
    "autocheck.py",
    "run_tests.py",
    "build_release_zip.py",
}


def _drop_sitepackage(norm: str) -> bool:
    if not norm.startswith(SP):
        return False
    entry = norm[len(SP):].split("/", 1)[0]
    for name in DROP_PACKAGES:
        # the package itself, its .py module, its .libs folder and dist-info
        if (entry == name or entry == name + ".py" or entry == name + ".libs"
                or entry.startswith(name + "-")):
            return True
    return False


def _skip(path: str) -> bool:
    norm = path.replace("\\", "/")
    if any(norm == p or norm.startswith(p) for p in TK_SKIP):
        return True
    # Dev-only files: the tests, the test runner and the release builder are
    # for the repository, not for the end user. The archive must contain
    # exactly what the program needs to run (user rule 2026-09-08).
    if norm in DEV_ONLY or norm.startswith("test_"):
        return True
    # .pyc/__pycache__ is dead weight (~13 MB in the zip): pythonw regenerates
    # them on the fly, a distribution does not need them.
    if norm.endswith(".pyc") or "/__pycache__/" in norm:
        return True
    return _drop_sitepackage(norm)


for root, _dirs, fs in os.walk("runtime"):
    for f in fs:
        path = os.path.join(root, f)
        if _skip(path):
            continue
        extra.append(path)

seen = set()
uniq = []
for f in files + extra:
    norm = f.replace("\\", "/")
    if norm in seen:
        continue
    seen.add(norm)
    # The same dev-only filter applies to the git-tracked files: the
    # tests and the builders must not reach the archive (user rule
    # 2026-09-08).
    if _skip(norm):
        continue
    uniq.append(norm)

out = "neuralscreen-v1.3.0-full.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for f in uniq:
        if not os.path.isfile(f):
            print("MISSING:", f)
            continue
        # config.json comes ONLY from git HEAD, never from disk: the working
        # copy holds the developer's personal menu_offset/menu_scale/theme and
        # those must not ship. Comparing against the worktree is NOT enough —
        # a staged personal config (git add) would make the diff clean and the
        # personal values would leak into the zip (audit #2, R1).
        if f == "config.json":
            try:
                data = subprocess.check_output(["git", "show", "HEAD:config.json"])
                z.writestr(f, data)
            except subprocess.CalledProcessError:
                # config.json has never been committed — take it from disk.
                z.write(f, f)
            continue
        z.write(f, f)
print("entries:", len(uniq))
print("size:", os.path.getsize(out))
