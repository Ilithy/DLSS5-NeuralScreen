"""Сборка релизного архива NeuralScreen v1.1.0: git-файлы + артефакты + runtime."""
import os
import subprocess
import zipfile

files = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
extra = [
    "NeuralScreen.vbs",
    "README.ru.md",
    "native/nvngx.dll",
    "native/nvngx_dlssnr.dll",
]
# tcl/tk в архив не кладём: окно настроек на tkinter убрано, интерфейс
# целиком живёт в оверлейном меню. Ничего из проекта tkinter не импортирует
# (PIL/_tkinter_finder тянет его лениво и только для ImageTk).
TK_SKIP = ("runtime/tcl/", "runtime/tcl86t.dll", "runtime/tk86t.dll",
           "runtime/_tkinter.pyd", "runtime/Lib/tkinter/")

# Рантайм собирался под другие задачи и тащит пакеты, которых программа не
# касается: веб-морду, таблицы, упаковщик. Проверено через sys.modules после
# импорта всех модулей проекта — нужны только av, cv2, numpy, PIL, pygame,
# pystray, dxcam, comtypes. Остальное вырезаем, это ~130 МБ до сжатия.
DROP_PACKAGES = {
    # gradio и его окружение
    "gradio", "gradio_client", "hf_gradio", "huggingface_hub", "hf_xet",
    "fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core",
    "annotated_types", "annotated_doc", "typing_inspection",
    "safehttpx", "groovy", "pydub", "python_multipart", "multipart",
    "orjson", "httpx", "httpcore", "h11", "anyio", "idna", "certifi",
    "fsspec", "filelock", "jinja2", "markupsafe", "tqdm", "audioop",
    "audioop_lts", "brotli", "_brotli", "yaml", "_yaml", "pyyaml",
    "semantic_version", "tomlkit", "typer", "click", "shellingham",
    "rich", "markdown_it", "markdown_it_py", "mdurl", "pygments",
    # таблицы и время
    "pandas", "pytz", "tzdata", "dateutil", "python_dateutil",
    # упаковщик
    "PyInstaller", "pyinstaller", "_pyinstaller_hooks_contrib",
    "pyinstaller_hooks_contrib", "altgraph", "pefile", "peutils", "ordlookup",
    "psutil",
    # менеджер пакетов конечному пользователю не нужен
    "pip",
}
SP = "runtime/Lib/site-packages/"


def _drop_sitepackage(norm: str) -> bool:
    if not norm.startswith(SP):
        return False
    entry = norm[len(SP):].split("/", 1)[0]
    for name in DROP_PACKAGES:
        # сам пакет, его .py-модуль, папка .libs и dist-info рядом
        if (entry == name or entry == name + ".py" or entry == name + ".libs"
                or entry.startswith(name + "-")):
            return True
    return False


def _skip(path: str) -> bool:
    norm = path.replace("\\", "/")
    if any(norm == p or norm.startswith(p) for p in TK_SKIP):
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
    uniq.append(norm)

out = "neuralscreen-v1.1.0-full.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for f in uniq:
        if not os.path.isfile(f):
            print("MISSING:", f)
            continue
        # config.json — из git, а не с диска: на диске лежат персональные
        # menu_offset/menu_scale разработчика, в архив они не должны попадать.
        if f == "config.json":
            r = subprocess.run(["git", "diff", "--quiet", "--", "config.json"])
            if r.returncode != 0:
                data = subprocess.check_output(
                    ["git", "show", "HEAD:config.json"])
                z.writestr(f, data)
                continue
        z.write(f, f)
print("entries:", len(uniq))
print("size:", os.path.getsize(out))
